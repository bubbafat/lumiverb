"""CLI tests: config, library create/list, download. All use mocks; no real HTTP or DB."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from src.cli.main import app

runner = CliRunner()


@pytest.mark.fast
def test_config_set_and_show(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Set config, show config, assert values match."""
    config_file = tmp_path / "config.json"
    monkeypatch.setattr("src.cli.config._config_path", lambda: config_file)

    result_set = runner.invoke(
        app,
        ["config", "set", "--api-url", "http://test.example.com", "--api-key", "sk_test_xyz"],
    )
    assert result_set.exit_code == 0

    result_show = runner.invoke(app, ["config", "show"])
    assert result_show.exit_code == 0
    assert "http://test.example.com" in result_show.output
    assert "[set]" in result_show.output


@pytest.mark.fast
def test_library_create_prints_id() -> None:
    """Mock client.post; assert output contains lib_."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "library_id": "lib_01HXYZ",
        "name": "My Library",
        "root_path": "/photos",
            }
    mock_client = MagicMock()
    mock_client.post.return_value = mock_response

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(
            app,
            [
                "library",
                "create",
                "--name",
                "My Library",
                "--path",
                "/photos",
            ],
        )

    assert result.exit_code == 0
    assert "lib_" in result.output
    assert "My Library" in result.output
    mock_client.post.assert_called_once()
    call_kw = mock_client.post.call_args[1]
    assert call_kw["json"] == {"name": "My Library", "root_path": "/photos"}


@pytest.mark.fast
def test_library_list_shows_table() -> None:
    """Mock client.get returning two libraries; assert both names appear in output."""
    mock_response = MagicMock()
    mock_response.json.return_value = [
        {
            "library_id": "lib_01A",
            "name": "First Lib",
            "root_path": "/path/a",
                        "last_scan_at": None,
        },
        {
            "library_id": "lib_01B",
            "name": "Second Lib",
            "root_path": "/path/b",
            "last_scan_at": "2025-01-15T10:00:00",
        },
    ]
    mock_client = MagicMock()
    mock_client.get.return_value = mock_response

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["library", "list"])

    assert result.exit_code == 0
    assert "First Lib" in result.output
    assert "Second Lib" in result.output
    mock_client.get.assert_called_once_with("/v1/libraries")


@pytest.mark.fast
def test_library_delete_requires_confirmation() -> None:
    """Mock client.get to return one library, mock input to return 'n'; assert DELETE never called, exit code 0."""
    mock_response = MagicMock()
    mock_response.json.return_value = [
        {"library_id": "lib_01DEL", "name": "ToDelete", "root_path": "/path", "status": "active"},
    ]
    mock_client = MagicMock()
    mock_client.get.return_value = mock_response

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(
            app,
            ["library", "delete", "--name", "ToDelete"],
            input="n",
        )

    assert result.exit_code == 0
    assert "Aborted" in result.output
    mock_client.delete.assert_not_called()


@pytest.mark.fast
def test_library_delete_confirms_and_calls_api() -> None:
    """Mock client.get to return one library, mock input to return 'y'; assert DELETE /v1/libraries/{id} called, success message printed."""
    mock_response = MagicMock()
    mock_response.json.return_value = [
        {"library_id": "lib_01DEL", "name": "ToDelete", "root_path": "/path", "status": "active"},
    ]
    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.delete.return_value = MagicMock(status_code=204)

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(
            app,
            ["library", "delete", "--name", "ToDelete"],
            input="y",
        )

    assert result.exit_code == 0
    assert "moved to trash" in result.output
    assert "empty-trash" in result.output
    mock_client.delete.assert_called_once()
    call_args = mock_client.delete.call_args[0]
    assert call_args[0] == "/v1/libraries/lib_01DEL"


@pytest.mark.fast
def test_library_empty_trash_aborts_if_none() -> None:
    """Mock GET /v1/libraries?include_trashed=true to return []; assert 'Trash is empty.' printed, exit 0."""
    mock_response = MagicMock()
    mock_response.json.return_value = []
    mock_client = MagicMock()
    mock_client.get.return_value = mock_response

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["library", "empty-trash"])

    assert result.exit_code == 0
    assert "Trash is empty" in result.output
    mock_client.get.assert_called_once()
    assert mock_client.get.call_args[1]["params"] == {"include_trashed": True}
    mock_client.post.assert_not_called()


@pytest.mark.fast
def test_library_empty_trash_requires_confirmation() -> None:
    """Mock GET to return one trashed library, mock input to return 'n'; assert POST /v1/libraries/empty-trash never called."""
    mock_response = MagicMock()
    mock_response.json.return_value = [
        {"library_id": "lib_trash1", "name": "TrashedLib", "root_path": "/x", "status": "trashed"},
    ]
    mock_client = MagicMock()
    mock_client.get.return_value = mock_response

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["library", "empty-trash"], input="n")

    assert result.exit_code == 0
    assert "Aborted" in result.output
    mock_client.post.assert_not_called()


@pytest.mark.fast
def test_download_refuses_tty_without_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """When stdout is a TTY and --output is omitted, command should refuse to write binary."""
    # Pretend stdout is a TTY by patching the isatty method on the real stdout object.
    import sys
    import typer

    from src.cli.main import download, console

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)

    # Capture console output.
    messages: list[str] = []

    def _print(*args, **kwargs):
        msg = "".join(str(a) for a in args)
        messages.append(msg)

    monkeypatch.setattr(console, "print", _print)

    # Patch client, though it should not be used before the TTY guard triggers.
    mock_client = MagicMock()

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        with pytest.raises(typer.Exit) as exc:
            download(
                library="TestLib",
                asset_id=None,
                path="Photos/one.jpg",
                size="proxy",
                output=None,
            )

    assert exc.value.exit_code == 1
    combined = "\n".join(messages)
    assert "Refusing to write binary to terminal" in combined


@pytest.mark.fast
def test_download_saves_to_file(tmp_path: Path) -> None:
    """Download with --output file writes streamed bytes and prints Saved message."""
    from httpx import Response, Request

    mock_client = MagicMock()

    # Resolve library_id
    libs_resp = MagicMock()
    libs_resp.json.return_value = [{"library_id": "lib_1", "name": "TestLib"}]
    # Asset lookup by path
    asset_lookup_resp = MagicMock()
    asset_lookup_resp.status_code = 200
    asset_lookup_resp.json.return_value = {"asset_id": "ast_123"}

    def _get_side_effect(path: str, **kwargs):
        if path == "/v1/libraries":
            return libs_resp
        if path == "/v1/assets/by-path":
            return asset_lookup_resp
        if path == "/v1/assets/ast_123":
            meta = MagicMock()
            meta.json.return_value = {"rel_path": "Photos/one.jpg"}
            return meta
        raise AssertionError(f"Unexpected GET path {path}")

    mock_client.get.side_effect = _get_side_effect

    # Streaming response
    req = Request("GET", "http://example.com")
    stream_resp = Response(200, request=req)
    chunks = [b"abc", b"def"]

    def _iter_bytes(chunk_size: int = 65536):
        for c in chunks:
            yield c

    stream_resp.iter_bytes = _iter_bytes  # type: ignore[assignment]

    from contextlib import contextmanager

    @contextmanager
    def _stream_ctx(path: str, **kwargs):
        yield stream_resp

    mock_client.stream.side_effect = _stream_ctx

    out_file = tmp_path / "out.jpg"

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(
            app,
            [
                "download",
                "--library",
                "TestLib",
                "--path",
                "Photos/one.jpg",
                "--output",
                str(out_file),
            ],
        )

    assert result.exit_code == 0
    assert out_file.read_bytes() == b"abcdef"
    assert "Saved to" in result.output


@pytest.mark.fast
def test_download_saves_to_directory(tmp_path: Path) -> None:
    """Download with --output dir/ derives filename from rel_path."""
    from httpx import Response, Request

    mock_client = MagicMock()

    libs_resp = MagicMock()
    libs_resp.json.return_value = [{"library_id": "lib_1", "name": "TestLib"}]
    asset_lookup_resp = MagicMock()
    asset_lookup_resp.status_code = 200
    asset_lookup_resp.json.return_value = {"asset_id": "ast_123"}

    asset_meta_resp = MagicMock()
    asset_meta_resp.json.return_value = {"rel_path": "Photos/UK2024/DSC07171.ARW"}

    def _get_side_effect(path: str, **kwargs):
        if path == "/v1/libraries":
            return libs_resp
        if path == "/v1/assets/by-path":
            return asset_lookup_resp
        if path == "/v1/assets/ast_123":
            return asset_meta_resp
        raise AssertionError(f"Unexpected GET path {path}")

    mock_client.get.side_effect = _get_side_effect

    req = Request("GET", "http://example.com")
    stream_resp = Response(200, request=req)
    chunks = [b"\x00" * 4]

    def _iter_bytes(chunk_size: int = 65536):
        for c in chunks:
            yield c

    stream_resp.iter_bytes = _iter_bytes  # type: ignore[assignment]

    from contextlib import contextmanager

    @contextmanager
    def _stream_ctx(path: str, **kwargs):
        yield stream_resp

    mock_client.stream.side_effect = _stream_ctx

    out_dir = tmp_path / "out"

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(
            app,
            [
                "download",
                "--library",
                "TestLib",
                "--asset-id",
                "ast_123",
                "--output",
                str(out_dir) + "/",
            ],
        )

    assert result.exit_code == 0
    expected_file = out_dir / "DSC07171_proxy.jpg"
    assert expected_file.exists()
    assert expected_file.read_bytes() == b"\x00" * 4
