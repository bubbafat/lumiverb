"""CLI tests for similar command."""

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from src.cli.main import app

runner = CliRunner()


@pytest.mark.fast
def test_similar_invalid_output_exits_1() -> None:
    """--output other than table/json/text prints error and exits 1."""
    mock_client = MagicMock()
    mock_client.get.return_value.json.return_value = [{"library_id": "lib_1", "name": "Lib", "root_path": "/path"}]

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(
            app,
            ["similar", "ast_abc", "-l", "Lib", "--output", "xml"],
        )

    assert result.exit_code == 1
    assert "table, json, text" in result.output
    mock_client.get.assert_not_called()


@pytest.mark.fast
def test_similar_calls_api_with_asset_id_library_id_limit_offset() -> None:
    """Resolve library by name, then GET /v1/similar with asset_id, library_id, limit (default 10), offset."""
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        MagicMock(json=lambda: [{"library_id": "lib_xyz", "name": "MyLib", "root_path": "/x"}]),
        MagicMock(
            status_code=200,
            json=lambda: {
                "source_asset_id": "ast_src",
                "hits": [
                    {
                        "asset_id": "ast_1",
                        "rel_path": "photos/one.jpg",
                        "thumbnail_key": None,
                        "proxy_key": None,
                        "distance": 0.12,
                    }
                ],
                "total": 1,
                "embedding_available": True,
            },
        ),
    ]

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["similar", "ast_src", "--library", "MyLib"])

    assert result.exit_code == 0
    assert mock_client.get.call_count == 2
    assert mock_client.get.call_args_list[0][0][0] == "/v1/libraries"
    call_args = mock_client.get.call_args_list[1]
    assert call_args[0][0] == "/v1/similar"
    assert call_args[1]["params"]["asset_id"] == "ast_src"
    assert call_args[1]["params"]["library_id"] == "lib_xyz"
    assert call_args[1]["params"]["limit"] == 10
    assert call_args[1]["params"]["offset"] == 0
    assert "photos/one.jpg" in result.output
    assert "0.1200" in result.output


@pytest.mark.fast
def test_similar_no_embeddings_exit_0() -> None:
    """Empty hits and embedding_available=False: print message about no embeddings, exit 0."""
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        MagicMock(json=lambda: [{"library_id": "lib_1", "name": "EmptyLib", "root_path": "/path"}]),
        MagicMock(
            status_code=200,
            json=lambda: {
                "source_asset_id": "ast_foo",
                "hits": [],
                "total": 0,
                "embedding_available": False,
            },
        ),
    ]

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["similar", "ast_foo", "-l", "EmptyLib"])

    assert result.exit_code == 0
    assert "No similar assets" in result.output
    assert "no embeddings" in result.output


@pytest.mark.fast
def test_similar_no_hits_exit_0() -> None:
    """Empty hits with embedding_available=True: print 'No similar assets.', exit 0."""
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        MagicMock(json=lambda: [{"library_id": "lib_1", "name": "Lib", "root_path": "/path"}]),
        MagicMock(
            status_code=200,
            json=lambda: {
                "source_asset_id": "ast_bar",
                "hits": [],
                "total": 0,
                "embedding_available": True,
            },
        ),
    ]

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["similar", "ast_bar", "-l", "Lib"])

    assert result.exit_code == 0
    assert "No similar assets." in result.output


@pytest.mark.fast
def test_similar_table_output() -> None:
    """Default table output shows Path, Distance, Asset ID and similar count."""
    hit = {
        "asset_id": "ast_2",
        "rel_path": "folder/image.jpg",
        "thumbnail_key": None,
        "proxy_key": None,
        "distance": 0.05,
    }
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        MagicMock(json=lambda: [{"library_id": "lib_1", "name": "T", "root_path": "/"}]),
        MagicMock(
            status_code=200,
            json=lambda: {
                "source_asset_id": "ast_1",
                "hits": [hit],
                "total": 1,
                "embedding_available": True,
            },
        ),
    ]

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["similar", "ast_1", "-l", "T"])

    assert result.exit_code == 0
    assert "folder/image.jpg" in result.output
    assert "0.0500" in result.output
    assert "ast_2" in result.output
    assert "1 similar asset(s)" in result.output


@pytest.mark.fast
def test_similar_json_output() -> None:
    """--output json prints full API response as JSON."""
    hit = {
        "asset_id": "ast_2",
        "rel_path": "a/b.jpg",
        "thumbnail_key": None,
        "proxy_key": None,
        "distance": 0.2,
    }
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        MagicMock(json=lambda: [{"library_id": "lib_1", "name": "J", "root_path": "/"}]),
        MagicMock(
            status_code=200,
            json=lambda: {
                "source_asset_id": "ast_1",
                "hits": [hit],
                "total": 1,
                "embedding_available": True,
            },
        ),
    ]

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["similar", "ast_1", "-l", "J", "-o", "json"])

    assert result.exit_code == 0
    assert "source_asset_id" in result.output
    assert "ast_1" in result.output
    assert "a/b.jpg" in result.output
    assert "embedding_available" in result.output


@pytest.mark.fast
def test_similar_text_output() -> None:
    """--output text prints one rel_path per line."""
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        MagicMock(json=lambda: [{"library_id": "lib_1", "name": "Txt", "root_path": "/"}]),
        MagicMock(
            status_code=200,
            json=lambda: {
                "source_asset_id": "ast_1",
                "hits": [
                    {"asset_id": "ast_a", "rel_path": "p1.jpg", "thumbnail_key": None, "proxy_key": None, "distance": 0.1},
                    {"asset_id": "ast_b", "rel_path": "p2.jpg", "thumbnail_key": None, "proxy_key": None, "distance": 0.2},
                ],
                "total": 2,
                "embedding_available": True,
            },
        ),
    ]

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["similar", "ast_1", "-l", "Txt", "-o", "text"])

    assert result.exit_code == 0
    assert "p1.jpg" in result.output
    assert "p2.jpg" in result.output


@pytest.mark.fast
def test_similar_limit_and_offset_passed_to_api() -> None:
    """--limit and --offset are passed as query params to /v1/similar."""
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        MagicMock(json=lambda: [{"library_id": "lib_1", "name": "L", "root_path": "/"}]),
        MagicMock(
            status_code=200,
            json=lambda: {"source_asset_id": "ast_1", "hits": [], "total": 0, "embedding_available": True},
        ),
    ]

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        runner.invoke(app, ["similar", "ast_1", "-l", "L", "--limit", "5", "--offset", "3"])

    call_args = mock_client.get.call_args_list[1]
    assert call_args[1]["params"]["limit"] == 5
    assert call_args[1]["params"]["offset"] == 3


@pytest.mark.fast
def test_similar_image_calls_api_and_prints_table(tmp_path) -> None:
    """similar-image resizes, encodes, resolves library, and POSTs payload."""
    from PIL import Image as PILImage

    img_path = tmp_path / "query.jpg"
    PILImage.new("RGB", (4000, 3000), color=(255, 0, 0)).save(img_path, format="JPEG")

    mock_client = MagicMock()
    mock_client.get.return_value.json.return_value = [
        {"library_id": "lib_img", "name": "ImgLib", "root_path": "/x"}
    ]
    mock_client.post.return_value.json.return_value = {
        "hits": [
            {
                "asset_id": "ast_1",
                "rel_path": "photo.jpg",
                "thumbnail_key": None,
                "proxy_key": None,
                "distance": 0.1234,
            }
        ],
        "total": 1,
    }

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(
            app,
            [
                "similar-image",
                str(img_path),
                "--library",
                "ImgLib",
                "--limit",
                "10",
                "--offset",
                "0",
            ],
        )

    assert result.exit_code == 0
    # First call lists libraries
    mock_client.get.assert_called_once_with("/v1/libraries")
    # Second call posts search-by-image
    assert mock_client.post.call_args[0][0] == "/v1/similar/search-by-image"
    payload = mock_client.post.call_args[1]["json"]
    assert payload["library_id"] == "lib_img"
    assert payload["limit"] == 10
    assert payload["offset"] == 0
    assert "image_b64" in payload and isinstance(payload["image_b64"], str)
    # Output table shows path, distance, and total
    assert "photo.jpg" in result.output
    assert "0.1234" in result.output
    assert "1 result(s)" in result.output


@pytest.mark.fast
def test_similar_image_json_output(tmp_path) -> None:
    """--output json prints full response as JSON."""
    from PIL import Image as PILImage

    img_path = tmp_path / "query.jpg"
    PILImage.new("RGB", (100, 100), color=(0, 255, 0)).save(img_path, format="JPEG")

    mock_client = MagicMock()
    mock_client.get.return_value.json.return_value = [
        {"library_id": "lib_json", "name": "JsonLib", "root_path": "/x"}
    ]
    mock_client.post.return_value.json.return_value = {
        "hits": [
            {
                "asset_id": "ast_2",
                "rel_path": "img.jpg",
                "thumbnail_key": None,
                "proxy_key": None,
                "distance": 0.5,
            }
        ],
        "total": 1,
    }

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(
            app,
            [
                "similar-image",
                str(img_path),
                "--library",
                "JsonLib",
                "--output",
                "json",
            ],
        )

    assert result.exit_code == 0
    assert '"hits"' in result.output
    assert '"total"' in result.output
    assert '"img.jpg"' in result.output
