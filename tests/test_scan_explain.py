# ruff: noqa: F811 — pytest fixtures are named as parameters
"""`lumiverb scan --explain`: every difference between the disk and what Lumiverb shows.

A dry run that also lists, grouped and a path a line, each media file on
disk Lumiverb doesn't show and why (removed by a person, left out by a path
filter, a name it can't take, empty, still being written, not added yet,
changed, moved), and each clip whose file isn't on disk (not found by this
scan, archived already as missing). The real run_scan, over a real folder of
real files, against the real API and its database; the server's side is in
tests/test_scans_api.py (GET /v1/scans/{id}/skipped and /gone).
"""

from __future__ import annotations

import io
import time
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from src.processing.ingest import _walk_library
from src.processing.scan import ScanStats, _listed, run_scan
from src.shared.path_filter import PathFilter
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_scan_outcomes import (  # noqa: F401 — fixtures
    HOUR,
    Lib,
    _ago,
    _cache_here,
    _image,
    _in_sight,
    _move,
    _remove,
    _rows,
    _scan,
    _set_mtime,
    api,
    lib,
)


def _explain(api, lib: Lib, **options) -> str:
    """What `lumiverb scan --explain` prints for the library, as the CLI runs it (moves asked about)."""
    out = io.StringIO()
    run_scan(api, lib.library, explain=True, concurrency=1, console=Console(file=out, width=200), **options)
    return out.getvalue()


def _listing(printed: str) -> str:
    """The explanation's own lines: from its first heading to the dry run's summary."""
    start = min(i for i in (printed.find("On disk, not shown"), printed.find("In Lumiverb, not on disk"),
                            printed.find("Nothing differs")) if i >= 0)
    return printed[start:printed.index("Scan summary")].strip() + "\n"


@pytest.mark.slow
def test_explain_lists_every_difference_and_changes_nothing(env, api, lib):
    client, headers, *_ = env
    for n, rel in enumerate(["a.png", "t.png", "r.png", "e.png", "gone.png", "old.png", "m.png", "c.png"]):
        _image(lib, rel, n)
    _scan(api, lib)
    clips = {rel: c["asset_id"] for rel, c in _in_sight(env, lib).items()}
    _remove(env, clips["t.png"], "trashed")
    _remove(env, clips["r.png"], "archived")
    _remove(env, clips["e.png"], "deleted for good")
    (lib.root / "old.png").unlink()
    _scan(api, lib)  # archived as missing

    # Since the last scan:
    (lib.root / "gone.png").unlink()
    _move(lib, "m.png", "moved/m.png")
    _image(lib, "c.png", 100, mtime=_ago(HOUR / 2))  # other content
    _image(lib, "new.png", 101)
    _image(lib, "[x].png", 102)  # rich markup in a name
    _image(lib, "drafts/b.png", 103)
    r = client.post(f"/v1/libraries/{lib.id}/filters", json={"type": "exclude", "pattern": "drafts/**"},
                    headers=headers)
    assert r.status_code == 201, r.text
    (lib.root / "empty.png").write_bytes(b"")
    _set_mtime(lib.root / "empty.png", _ago(HOUR))
    _image(lib, "fresh.png", 104, mtime=time.time())
    _image(lib, "bad\nname.png", 105)
    _image(lib, "tab\tfolder/x.png", 106)
    _image(lib, "back\\slash.png", 107)
    before = _rows(env, lib)

    printed = _explain(api, lib)
    assert _listing(printed) == (
        "On disk, not shown in Lumiverb:\n"
        "  Deleted for good (1): a person emptied the trash or deleted them forever; "
        "lumiverb scan --force-add brings them back\n"
        "    e.png\n"
        "  Archived by a person (1): lumiverb scan --force-add brings them back\n"
        "    r.png\n"
        "  1 more in the trash: --include-trash lists them\n"
        "  Left out by a path filter (1)\n"
        "    drafts/b.png\n"
        "  Names Lumiverb can't take (3): a control character, a backslash, or too long\n"
        "    back\\slash.png\n"
        "    'bad\\nname.png'\n"
        "    'tab\\tfolder/'\n"
        "  Empty files (1): 0 bytes, never added\n"
        "    empty.png\n"
        "  Still being written (1): a later scan takes them\n"
        "    fresh.png\n"
        "  Not added yet (2): the next scan adds these\n"
        "    [x].png\n"
        "    new.png\n"
        "  Changed on disk (1): the next scan reads them again\n"
        "    c.png\n"
        "  Moved or renamed (1)\n"
        "    m.png → moved/m.png\n"
        "\n"
        "In Lumiverb, not on disk:\n"
        "  Not found (1): the next scan archives these as missing\n"
        "    gone.png\n"
        "  Archived because their files went missing (1)\n"
        "    old.png\n"
    )
    assert "moved file(s) detected" not in printed  # the moves are listed once, with the rest

    with_trash = _listing(_explain(api, lib, include_trash=True))
    assert "more in the trash" not in with_trash
    assert ("  Archived by a person (1): lumiverb scan --force-add brings them back\n"
            "    r.png\n"
            "  In the trash (1): restore them from the trash to bring them back\n"
            "    t.png\n"
            "  Left out by a path filter (1)\n") in with_trash

    # A media type's: only its files and clips (a folder can't be told apart).
    assert _listing(_explain(api, lib, media_type_filter="video")) == (
        "On disk, not shown in Lumiverb:\n"
        "  Names Lumiverb can't take (1): a control character, a backslash, or too long\n"
        "    'tab\\tfolder/'\n"
    )
    assert _rows(env, lib) == before  # nothing changed


@pytest.mark.slow
def test_explain_with_force_add_lists_what_it_would_bring_back(env, api, lib):
    """--explain --force-add: what a person removed is what the scan would
    add, in a group of its own, never "the next scan adds these" (a plain
    scan wouldn't); and nothing changes."""
    for n, rel in enumerate(["t.png", "r.png", "e.png", "keep.png"]):
        _image(lib, rel, n)
    _scan(api, lib)
    clips = {rel: c["asset_id"] for rel, c in _in_sight(env, lib).items()}
    _remove(env, clips["t.png"], "trashed")
    _remove(env, clips["r.png"], "archived")
    _remove(env, clips["e.png"], "deleted for good")
    _image(lib, "new.png", 10)
    before = _rows(env, lib)

    printed = _explain(api, lib, force_add=True)
    assert _listing(printed) == (
        "On disk, not shown in Lumiverb:\n"
        "  Brought back by --force-add (3): a person removed them; this scan would add them\n"
        "    e.png\n"
        "    r.png\n"
        "    t.png\n"
        "  Not added yet (1): the next scan adds these\n"
        "    new.png\n"
    )
    assert "Would bring back 2 clip(s) you removed" in printed
    assert _rows(env, lib) == before  # nothing changed


@pytest.mark.slow
def test_explain_says_so_when_nothing_differs(env, api, lib):
    _image(lib, "a.png", 1)
    _image(lib, "day 1/b.png", 2)
    _scan(api, lib)
    assert _listing(_explain(api, lib, include_trash=True)) == (
        "Nothing differs: every media file on disk is in Lumiverb, and every clip's file is on disk.\n"
    )


@pytest.mark.slow
def test_explain_says_why_the_clips_not_found_would_be_kept(env, api, lib):
    _image(lib, "a.png", 1)
    _image(lib, "b.png", 2)
    _scan(api, lib)
    (lib.root / "b.png").unlink()
    assert ("  Not found (1): kept, as --skip-moves archives nothing\n"
            "    b.png\n") in _listing(_explain(api, lib, skip_moves=True))


@pytest.mark.fast
def test_the_walk_names_what_it_leaves_out_when_asked(tmp_path):
    for rel in ("a.jpg", "drafts/b.jpg", "notes.txt", "a\rb.jpg", "Day\x7f/c.jpg", "x\ty.txt", "Day/d.mov"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    filtered: list[str] = []
    refused: list[str] = []
    walked = _walk_library(tmp_path, library_filters=[PathFilter(type="exclude", pattern="drafts/**")],
                           filtered=filtered, refused_names=refused)
    assert [f["rel_path"] for f in walked] == ["Day/d.mov", "a.jpg"]
    assert filtered == ["drafts/b.jpg"]
    assert sorted(refused) == ["Day\x7f/", "a\rb.jpg"]  # media files and folders, never other files


@pytest.mark.fast
def test_a_listing_is_followed_a_page_at_a_time():
    pages = {None: {"items": [{"n": 1}], "next_cursor": "a", "total": 3},
             "a": {"items": [{"n": 2}], "next_cursor": "b", "total": 3},
             "b": {"items": [{"n": 3}], "next_cursor": None, "total": 3}}
    asked: list[dict] = []

    def get(path: str, params: dict) -> MagicMock:
        asked.append(params)
        return MagicMock(json=MagicMock(return_value=pages[params.get("after")]))

    client = MagicMock(get=get)
    items = _listed(client, "/v1/archive", "items", Console(file=io.StringIO()), "Missing", {"kind": "missing"})
    assert items == [{"n": 1}, {"n": 2}, {"n": 3}]
    assert asked == [{"kind": "missing", "limit": 500}, {"kind": "missing", "limit": 500, "after": "a"},
                     {"kind": "missing", "limit": 500, "after": "b"}]


@pytest.mark.fast
def test_include_trash_goes_with_explain():
    from typer.testing import CliRunner

    from src.client.cli.main import app

    result = CliRunner().invoke(app, ["scan", "--library", "test", "--include-trash"])
    assert result.exit_code == 1
    assert "--include-trash goes with --explain" in result.output


@pytest.mark.fast
def test_explain_is_a_dry_run():
    from typer.testing import CliRunner

    from src.client.cli.main import app

    client = MagicMock()
    client.get.return_value.json.return_value = [{"library_id": "lib_1", "name": "test", "root_path": "/x"}]
    with (patch("src.client.cli.main.LumiverbClient", return_value=client),
          patch("src.processing.scan.run_scan", return_value=ScanStats()) as scan):
        result = CliRunner().invoke(app, ["scan", "--library", "test", "--explain", "--include-trash"])
    assert result.exit_code == 0, result.output
    asked = scan.call_args.kwargs
    assert (asked["dry_run"], asked["explain"], asked["include_trash"]) == (True, True, True)
    assert "Done:" not in result.output  # a dry run's summary is the scan's own
