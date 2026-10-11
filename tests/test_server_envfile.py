"""The install's env file, read as systemd reads an EnvironmentFile
(src/server/lifecycle/envfile.py): a Mac's launchd can't load it, so its
service reads it itself."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.server.lifecycle.envfile import EnvFileError, read_env_file

pytestmark = pytest.mark.fast


def _env(tmp_path: Path, text: str) -> dict[str, str]:
    path = tmp_path / "env"
    path.write_text(text)
    return read_env_file(path)


def test_the_brains_env_file_reads_as_systemd_reads_it(tmp_path):
    # As deploy-api.sh writes it: plain values, a URL with = in it, and the
    # root map as JSON in single quotes.
    env = _env(tmp_path, "\n".join([
        "# Lumiverb",
        "API_PORT=8100",
        "CONTROL_PLANE_DATABASE_URL=postgresql+psycopg2://app:se=cret@127.0.0.1:5434/control_plane",
        "",
        "; also a comment",
        """LUMIVERB_ROOT_MAP='{"/Volumes/MEDIA-01": "/mnt/media-01"}'""",
        "  BRANCH = main  ",
        'NAME="Robert\'s \\"Mac\\""',
    ]))
    assert env == {
        "API_PORT": "8100",
        "CONTROL_PLANE_DATABASE_URL": "postgresql+psycopg2://app:se=cret@127.0.0.1:5434/control_plane",
        "LUMIVERB_ROOT_MAP": '{"/Volumes/MEDIA-01": "/mnt/media-01"}',
        "BRANCH": "main",
        "NAME": 'Robert\'s "Mac"',
    }


def test_single_quotes_keep_backslashes(tmp_path):
    assert _env(tmp_path, r"PATTERN='a\nb'")["PATTERN"] == r"a\nb"


def test_an_empty_value_is_empty(tmp_path):
    assert _env(tmp_path, "QUICKWIT_URL=") == {"QUICKWIT_URL": ""}


@pytest.mark.parametrize("line", ["just words", "=value", "2KEY=value", "export KEY=value"])
def test_a_line_that_isnt_key_equals_value_is_refused_by_its_number(tmp_path, line):
    with pytest.raises(EnvFileError, match=r"line 2: not KEY=VALUE"):
        _env(tmp_path, f"OK=1\n{line}\n")
