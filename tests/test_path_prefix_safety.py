"""A path prefix that would leave the library is a clean error, never a traceback."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

pytestmark = pytest.mark.fast


@pytest.mark.parametrize("prefix", ["/etc", "../outside", "a/../../b"])
def test_scan_refuses_a_path_prefix_outside_the_library(prefix: str) -> None:
    from src.client.cli.main import app

    with patch("src.client.cli.main.LumiverbClient") as client:
        result = CliRunner().invoke(app, ["scan", "--library", "L", "--path-prefix", prefix])
    assert result.exit_code == 1
    assert "Invalid --path-prefix" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    client.assert_not_called()  # refused before anything is asked of the server


def test_run_scan_with_an_unsafe_prefix_removes_nothing(tmp_path: Path) -> None:
    from src.client.cli.scan import run_scan

    root = tmp_path / "lib"
    root.mkdir()
    (root / "f.jpg").touch()
    client = MagicMock()
    with (
        patch("src.client.cli.scan._load_tenant_filters", return_value=[]),
        patch("src.client.cli.scan._load_library_filters", return_value=[]),
    ):
        stats = run_scan(client, {"library_id": "lib_1", "root_path": str(root)}, path_prefix="../x",
                         console=Console(quiet=True), allow_moves=True)
    assert stats.unlisted == ["../x"]
    assert not [c for c in client.raw.call_args_list if c.args[:2] == ("DELETE", "/v1/assets")]


def test_scheduler_is_dir_says_no_for_a_path_outside_the_root(tmp_path: Path) -> None:
    from src.server.scheduler.scans import _is_dir

    (tmp_path / "lib").mkdir()
    (tmp_path / "outside").mkdir()
    assert _is_dir(tmp_path / "lib", "../outside") is False
    assert _is_dir(tmp_path, "lib") is True
