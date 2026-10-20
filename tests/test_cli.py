"""CLI tests: config, library create/list, download. All use mocks; no real HTTP or DB."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from src.client.cli.main import app

runner = CliRunner()


@pytest.mark.fast
def test_config_set_and_show(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Set config, show config, assert values match."""
    config_file = tmp_path / "config.json"
    monkeypatch.setattr("src.client.cli.config._config_path", lambda: config_file)

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

    with patch("src.client.cli.main.LumiverbClient", return_value=mock_client):
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
def test_library_create_says_why_an_editor_is_refused() -> None:
    """The API's 403 (choosing a library's folder is an admin's) is said on stderr, and nothing claims a library."""
    import httpx

    from src.client.cli.client import LumiverbAPIError, LumiverbClient

    def refuse(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/libraries"
        return httpx.Response(403, json={"error": {"code": "forbidden", "message": "Admin API key required",
                                                   "details": {}}})

    client = LumiverbClient(base_url="http://localhost:8000", token="editor-key")
    client._client = httpx.Client(transport=httpx.MockTransport(refuse))
    with patch("src.client.cli.main.LumiverbClient", return_value=client):
        result = runner.invoke(app, ["library", "create", "--name", "Mine", "--path", "/photos"])

    assert result.exit_code != 0
    assert isinstance(result.exception, LumiverbAPIError) and result.exception.status_code == 403
    assert "Error [forbidden]: Admin API key required" in result.stderr
    assert "Library created" not in result.output


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

    with patch("src.client.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["library", "list"])

    assert result.exit_code == 0
    assert "First Lib" in result.output
    assert "Second Lib" in result.output
    mock_client.get.assert_called_once_with("/v1/libraries")


@pytest.mark.fast
def test_library_delete_requires_confirmation() -> None:
    """Mock client.get to return one library, mock input to return 'n'; assert DELETE never called, exit code 0."""
    mock_client = MagicMock()
    mock_client.get.side_effect = _gets([
        {"library_id": "lib_01DEL", "name": "ToDelete", "root_path": "/path", "status": "active"},
    ])

    with patch("src.client.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(
            app,
            ["library", "delete", "--name", "ToDelete"],
            input="n",
        )

    assert result.exit_code == 0
    assert "Aborted" in result.output
    mock_client.raw.assert_not_called()


@pytest.mark.fast
def test_library_delete_confirms_and_calls_api() -> None:
    """Mock client.get to return one library, mock input to return 'y'; assert DELETE /v1/libraries/{id} called, success message printed."""
    mock_client = MagicMock()
    mock_client.get.side_effect = _gets([
        {"library_id": "lib_01DEL", "name": "ToDelete", "root_path": "/path", "status": "active"},
    ])
    mock_client.raw.return_value = MagicMock(status_code=204)

    with patch("src.client.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(
            app,
            ["library", "delete", "--name", "ToDelete"],
            input="y",
        )

    assert result.exit_code == 0
    assert "moved to trash" in result.output
    assert "empty-trash" in result.output
    mock_client.raw.assert_called_once()
    assert mock_client.raw.call_args[0] == ("DELETE", "/v1/libraries/lib_01DEL")


def _gets(libraries: list, archived: int = 0):
    """GET side effect: the library list, and the archive's total for one library."""
    def get(path, **kwargs):
        r = MagicMock()
        r.json.return_value = libraries if path == "/v1/libraries" else {"items": [], "total": archived}
        return r
    return get


def _archived_client(*responses, archived: int = 0):
    """A client whose library list has one active library and whose DELETEs answer `responses` in turn."""
    client = MagicMock()
    client.get.side_effect = _gets([{"library_id": "lib_1", "name": "Media", "root_path": "/m", "status": "active"}],
                                   archived)
    client.raw.side_effect = list(responses)
    return client


def _decision(code: str, details: dict) -> MagicMock:
    r = MagicMock(status_code=409)
    r.json.return_value = {"error": {"code": code, "message": code, "details": details}}
    return r


_DONE = MagicMock(status_code=204)
_USAGE = {"assets_in_projects": 2, "projects": [{"name": "Promo", "clips": 2, "status": "active"}],
          "other_projects": 0}


@pytest.mark.fast
def test_library_delete_asks_about_clips_in_projects() -> None:
    """The trash deletes the library for good on its own later, so the projects question comes now."""
    client = _archived_client(_decision("in_projects", _USAGE), _DONE)
    with patch("src.client.cli.main.LumiverbClient", return_value=client):
        result = runner.invoke(app, ["library", "delete", "--name", "Media"], input="y\ny\n")
    assert result.exit_code == 0, result.output
    assert "2 clips are in projects" in result.output and "Promo: 2" in result.output
    assert client.raw.call_args_list[0].kwargs["json"] == {"remove_from_projects": False}
    assert client.raw.call_args_list[-1].kwargs["json"] == {"remove_from_projects": True}
    assert "moved to trash" in result.output


@pytest.mark.fast
def test_library_delete_says_its_archived_clips_go_with_it() -> None:
    client = _archived_client(_DONE, archived=3)
    with patch("src.client.cli.main.LumiverbClient", return_value=client):
        result = runner.invoke(app, ["library", "delete", "--name", "Media"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "Its 3 archived clips go with it." in result.output


@pytest.mark.fast
def test_library_delete_cancelled_at_the_projects_question_changes_nothing() -> None:
    client = _archived_client(_decision("in_projects", _USAGE))
    with patch("src.client.cli.main.LumiverbClient", return_value=client):
        result = runner.invoke(app, ["library", "delete", "--name", "Media"], input="y\nn\n")
    assert result.exit_code == 0
    assert client.raw.call_count == 1


@pytest.mark.fast
def test_library_delete_with_flags_asks_nothing() -> None:
    client = _archived_client(_DONE)
    with patch("src.client.cli.main.LumiverbClient", return_value=client):
        result = runner.invoke(app, ["library", "delete", "--name", "Media", "--yes", "--remove-from-projects"])
    assert result.exit_code == 0, result.output
    assert client.raw.call_args.kwargs["json"] == {"remove_from_projects": True}


@pytest.mark.fast
def test_library_delete_yes_without_the_flag_stops_when_clips_are_in_projects() -> None:
    client = _archived_client(_decision("in_projects", _USAGE))
    with patch("src.client.cli.main.LumiverbClient", return_value=client):
        result = runner.invoke(app, ["library", "delete", "--name", "Media", "--yes"])
    assert result.exit_code == 2
    assert "--remove-from-projects" in result.output
    assert client.raw.call_count == 1


@pytest.mark.fast
def test_library_empty_trash_aborts_if_none() -> None:
    """Mock GET /v1/libraries?include_trashed=true to return []; assert 'Trash is empty.' printed, exit 0."""
    mock_response = MagicMock()
    mock_response.json.return_value = []
    mock_client = MagicMock()
    mock_client.get.return_value = mock_response

    with patch("src.client.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["library", "empty-trash", "--all"])

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
    # The read-only project-usage lookup runs before the question.
    mock_client.post.return_value.json.return_value = {"assets_in_projects": 0, "projects": [], "other_projects": 0}

    with patch("src.client.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["library", "empty-trash", "--all"], input="n")

    assert result.exit_code == 0
    assert "Aborted" in result.output
    assert [c.args[0] for c in mock_client.post.call_args_list] == ["/v1/assets/project-usage"]


@pytest.mark.fast
@pytest.mark.parametrize("args", [[], ["--name", "TrashedLib", "--all"]])
def test_library_empty_trash_needs_a_name_or_all(args) -> None:
    """Nothing deletes every library in the trash from a missing --name (Robert)."""
    mock_client = MagicMock()
    with patch("src.client.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["library", "empty-trash", *args])

    assert result.exit_code == 2
    assert "--all" in result.output
    mock_client.get.assert_not_called()
    mock_client.post.assert_not_called()


@pytest.mark.fast
def test_library_empty_trash_yes_asks_nothing() -> None:
    mock_response = MagicMock()
    mock_response.json.return_value = [
        {"library_id": "lib_trash1", "name": "TrashedLib", "root_path": "/x", "status": "trashed"},
        {"library_id": "lib_trash2", "name": "Other", "root_path": "/y", "status": "trashed"},
    ]
    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.post.return_value.json.return_value = {"assets_in_projects": 0, "projects": [], "other_projects": 0,
                                                       "deleted": 1}

    with patch("src.client.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["library", "empty-trash", "--name", "TrashedLib", "--yes"])

    assert result.exit_code == 0, result.output
    assert "Deleted 1 library." in result.output
    mock_client.post.assert_called_with("/v1/libraries/empty-trash",
                                        json={"library_ids": ["lib_trash1"], "remove_from_projects": False})


@pytest.mark.fast
@pytest.mark.parametrize("flags, posted", [([], False), (["--remove-from-projects"], True)])
def test_library_empty_trash_yes_with_clips_in_projects_needs_the_flag(flags, posted) -> None:
    """--yes asks no one, so clips in projects leave them only with --remove-from-projects."""
    mock_response = MagicMock()
    mock_response.json.return_value = [
        {"library_id": "lib_trash1", "name": "TrashedLib", "root_path": "/x", "status": "trashed"},
    ]
    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.post.return_value.json.return_value = {
        "assets_in_projects": 2, "projects": [{"name": "Reel", "clips": 2}], "other_projects": 0, "deleted": 1}

    with patch("src.client.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["library", "empty-trash", "--all", "--yes", *flags])

    paths = [c.args[0] for c in mock_client.post.call_args_list]
    if posted:
        assert result.exit_code == 0, result.output
        mock_client.post.assert_called_with("/v1/libraries/empty-trash",
                                            json={"library_ids": ["lib_trash1"], "remove_from_projects": True})
    else:
        assert result.exit_code == 2
        assert "--remove-from-projects" in result.output
        assert paths == ["/v1/assets/project-usage"]


@pytest.mark.fast
def test_download_refuses_tty_without_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """When stdout is a TTY and --output is omitted, command should refuse to write binary."""
    # Pretend stdout is a TTY by patching the isatty method on the real stdout object.
    import sys
    import typer

    from src.client.cli.main import download, console

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)

    # Capture console output.
    messages: list[str] = []

    def _print(*args, **kwargs):
        msg = "".join(str(a) for a in args)
        messages.append(msg)

    monkeypatch.setattr(console, "print", _print)

    # Patch client, though it should not be used before the TTY guard triggers.
    mock_client = MagicMock()

    with patch("src.client.cli.main.LumiverbClient", return_value=mock_client):
        with pytest.raises(typer.Exit) as exc:
            download(
                library="TestLib",
                asset_id=None,
                path="Photos/one.jpg",
                size="preview",
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

    with patch("src.client.cli.main.LumiverbClient", return_value=mock_client):
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

    with patch("src.client.cli.main.LumiverbClient", return_value=mock_client):
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


@pytest.mark.fast
@pytest.mark.parametrize(("size", "asked"), [
    (None, "lumiverb.preview"),
    ("preview", "lumiverb.preview"),
    ("lumiverb.preview.thumbnail", "lumiverb.preview.thumbnail"),
    ("preview.thumbnail", "lumiverb.preview.thumbnail"),
])
def test_download_asks_for_the_file_by_its_name(tmp_path: Path, size, asked) -> None:
    from contextlib import contextmanager

    from httpx import Request, Response

    mock_client = MagicMock()
    mock_client.get.side_effect = lambda path, **kwargs: MagicMock(
        json=MagicMock(return_value=[{"library_id": "lib_1", "name": "TestLib"}]))
    streamed: list[str] = []

    @contextmanager
    def _stream_ctx(path: str, **kwargs):
        streamed.append(path)
        resp = Response(200, request=Request("GET", "http://example.com"))
        resp.iter_bytes = lambda chunk_size=65536: iter([b"x"])  # type: ignore[assignment]
        yield resp

    mock_client.stream.side_effect = _stream_ctx
    with patch("src.client.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["download", "--library", "TestLib", "--asset-id", "ast_123",
                                     "--output", str(tmp_path / "out.jpg"), *(["--size", size] if size else [])])
    assert result.exit_code == 0, result.output
    assert streamed == [f"/v1/assets/ast_123/artifacts/{asked}"]
    assert "is called" not in result.output


@pytest.mark.fast
@pytest.mark.parametrize(("size", "name"), [("proxy", "preview"), ("thumbnail", "preview.thumbnail")])
def test_download_refuses_an_old_word_saying_the_name_to_type(tmp_path: Path, size, name) -> None:
    # Since ADR-024's part 5.
    with patch("src.client.cli.main.LumiverbClient") as client:
        result = runner.invoke(app, ["download", "--library", "TestLib", "--asset-id", "ast_123",
                                     "--output", str(tmp_path / "out.jpg"), "--size", size])
    assert result.exit_code == 1 and f"{size} is called {name} now: type that" in result.output
    client.assert_not_called()


@pytest.mark.fast
def test_download_refuses_a_size_that_isnt_a_file(tmp_path: Path) -> None:
    with patch("src.client.cli.main.LumiverbClient") as client:
        result = runner.invoke(app, ["download", "--library", "TestLib", "--asset-id", "ast_123",
                                     "--output", str(tmp_path / "out.jpg"), "--size", "huge"])
    assert result.exit_code == 1 and "preview, preview.thumbnail" in result.output
    client.assert_not_called()


def _closed_port() -> int:
    """A port on this machine nothing listens on."""
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.fast
@pytest.mark.parametrize("configured", [True, False])
def test_a_server_it_cant_reach_is_said_plainly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
                                                configured: bool) -> None:
    """Where it looked and how to say where the server is: no traceback."""
    import importlib
    import sys

    from src.client.cli import config as cli_config

    cli_main = importlib.import_module("src.client.cli.main")
    config_file = tmp_path / "config.json"
    monkeypatch.setattr(cli_config, "_config_path", lambda: config_file)
    url = f"http://127.0.0.1:{_closed_port()}"
    if configured:
        assert runner.invoke(app, ["config", "set", "--api-url", url, "--api-key", "k"]).exit_code == 0
    else:  # no file: the defaults, with a closed port for the default address (a Mac's install may be at the real one)
        defaults = lambda: cli_config.CLIConfig(api_url=url)  # noqa: E731
        monkeypatch.setattr(cli_config, "load_config", defaults)
        monkeypatch.setattr(cli_main, "load_config", defaults)
    monkeypatch.setattr(sys, "argv", ["lumiverb", "maintenance", "cleanup", "--all"])
    with pytest.raises(SystemExit) as exited:
        cli_main.main()
    assert exited.value.code == 1
    err = capsys.readouterr().err
    assert f"Can't reach Lumiverb at {url} (connection refused)." in err and "Traceback" not in err
    assert "lumiverb config set --api-url URL" in err
    assert ("No server is set for this CLI yet" in err) is not configured


@pytest.mark.fast
def test_with_nothing_set_it_looks_for_a_macs_install_on_this_machine() -> None:
    from src.client.cli.config import CLIConfig
    from src.server.lifecycle.init import WEB_PORT

    assert CLIConfig().api_url == f"http://localhost:{WEB_PORT}"
