"""Where a library's files live on this machine (ADR-016 phase 2).

The database keeps each library's ingest root as the editing machine sees
it, because exports point editors there. A machine that reaches the same
storage at another path (the brain mounts the DAS at /mnt/media-01) maps
the prefix in its own config, never in the database.
"""

from __future__ import annotations

import threading
import unicodedata
from pathlib import Path

import pytest
from typer.testing import CliRunner

from src.client.cli import roots
from src.client.cli.config import CLIConfig, load_config, save_config
from src.client.cli.roots import local_library_root, map_root, reachable_root

MAC = "/Volumes/media-01"


# ---------------------------------------------------------------------------
# map_root
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_no_mapping_keeps_the_root() -> None:
    assert map_root("/Volumes/media-01/Footage", {}) == "/Volumes/media-01/Footage"


@pytest.mark.fast
def test_exact_prefix_is_replaced() -> None:
    assert map_root(MAC, {MAC: "/mnt/media-01"}) == "/mnt/media-01"


@pytest.mark.fast
def test_folder_under_the_prefix_keeps_its_tail() -> None:
    assert map_root(f"{MAC}/Footage/2024", {MAC: "/mnt/media-01"}) == "/mnt/media-01/Footage/2024"


@pytest.mark.fast
def test_prefix_matches_whole_folders_only() -> None:
    # /Volumes/media-010 is another volume, not a folder of media-01.
    assert map_root("/Volumes/media-010/Footage", {MAC: "/mnt/media-01"}) == "/Volumes/media-010/Footage"


@pytest.mark.fast
def test_longest_prefix_wins() -> None:
    mapping = {"/Volumes": "/mnt/volumes", MAC: "/mnt/media-01"}
    assert map_root(f"{MAC}/Footage", mapping) == "/mnt/media-01/Footage"
    assert map_root("/Volumes/media-02/Photos", mapping) == "/mnt/volumes/media-02/Photos"


@pytest.mark.fast
def test_trailing_slashes_do_not_matter() -> None:
    assert map_root(f"{MAC}/Footage/", {f"{MAC}/": "/mnt/media-01/"}) == "/mnt/media-01/Footage"


@pytest.mark.fast
def test_unicode_forms_match() -> None:
    # macOS tab completion gives NFD; the server stores NFC.
    nfc = unicodedata.normalize("NFC", "/Volumes/Médias")
    nfd = unicodedata.normalize("NFD", nfc)
    assert map_root(f"{nfc}/Clips", {nfd: "/mnt/medias"}) == "/mnt/medias/Clips"


@pytest.mark.fast
def test_case_matters() -> None:
    # Paths are kept exactly as stored; a case-only match is a different path.
    assert map_root("/volumes/media-01/Footage", {MAC: "/mnt/media-01"}) == "/volumes/media-01/Footage"


@pytest.mark.fast
def test_root_prefix_maps_everything() -> None:
    assert map_root("/Footage/2024", {"/": "/mnt/mac"}) == "/mnt/mac/Footage/2024"


# ---------------------------------------------------------------------------
# local_library_root / reachable_root
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_local_root_uses_the_configured_mapping(tmp_path: Path) -> None:
    lib = {"library_id": "lib_1", "root_path": f"{MAC}/Footage"}
    assert local_library_root(lib, {MAC: str(tmp_path)}) == tmp_path / "Footage"


@pytest.mark.fast
def test_local_root_is_none_without_a_root_path() -> None:
    assert local_library_root({"library_id": "lib_1", "root_path": ""}, {}) is None


@pytest.mark.fast
def test_reachable_root_returns_the_mapped_folder(tmp_path: Path) -> None:
    (tmp_path / "Footage").mkdir()
    (tmp_path / "Footage" / "a.mov").write_bytes(b"x")
    lib = {"library_id": "lib_1", "root_path": f"{MAC}/Footage"}
    assert reachable_root(lib, root_map={MAC: str(tmp_path)}) == (tmp_path / "Footage").resolve()


@pytest.mark.fast
def test_missing_folder_is_unreachable(tmp_path: Path) -> None:
    lib = {"library_id": "lib_1", "root_path": f"{MAC}/Footage"}
    assert reachable_root(lib, root_map={MAC: str(tmp_path)}) is None


@pytest.mark.fast
def test_empty_folder_is_unreachable_when_entries_are_required(tmp_path: Path) -> None:
    # An unmounted mount point is an empty folder. Scanning it would mark
    # every file missing.
    lib = {"library_id": "lib_1", "root_path": str(tmp_path)}
    assert reachable_root(lib, root_map={}, require_entries=True) is None
    assert reachable_root(lib, root_map={}) == tmp_path.resolve()


@pytest.mark.fast
def test_a_hung_mount_times_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A network mount whose server is asleep can block a stat for minutes.
    release = threading.Event()

    def hang(path: Path, require_entries: bool) -> Path:
        release.wait(5)
        return path

    monkeypatch.setattr(roots, "_probe", hang)
    lib = {"library_id": "lib_1", "root_path": str(tmp_path)}
    try:
        assert reachable_root(lib, root_map={}, timeout=0.2) is None
    finally:
        release.set()


@pytest.mark.fast
def test_an_unreadable_folder_is_unreachable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def broken(path: Path, require_entries: bool) -> Path:
        raise OSError(112, "Host is down")

    monkeypatch.setattr(roots, "_probe", broken)
    lib = {"library_id": "lib_1", "root_path": str(tmp_path)}
    assert reachable_root(lib, root_map={}) is None


# ---------------------------------------------------------------------------
# Config and CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.mark.fast
def test_config_defaults_to_no_mapping(home: Path) -> None:
    assert load_config().root_map == {}


@pytest.mark.fast
def test_reachable_root_reads_the_saved_mapping(home: Path, tmp_path: Path) -> None:
    mount = tmp_path / "mnt"
    (mount / "Footage").mkdir(parents=True)
    save_config(CLIConfig(root_map={MAC: str(mount)}))
    lib = {"library_id": "lib_1", "root_path": f"{MAC}/Footage"}
    assert reachable_root(lib) == (mount / "Footage").resolve()


@pytest.mark.fast
def test_cli_map_root_saves_and_lists(home: Path, tmp_path: Path) -> None:
    from src.client.cli.main import app

    mount = tmp_path / "mnt"
    mount.mkdir()
    runner = CliRunner()
    result = runner.invoke(app, ["config", "map-root", f"{MAC}/", str(mount)])
    assert result.exit_code == 0, result.output
    assert load_config().root_map == {MAC: str(mount)}

    shown = runner.invoke(app, ["config", "show"])
    assert MAC in shown.output and str(mount) in shown.output


@pytest.mark.fast
def test_cli_map_root_needs_absolute_paths(home: Path) -> None:
    from src.client.cli.main import app

    result = CliRunner().invoke(app, ["config", "map-root", "Volumes/media-01", "/mnt/media-01"])
    assert result.exit_code != 0
    assert load_config().root_map == {}


@pytest.mark.fast
def test_cli_map_root_warns_when_the_target_is_not_there(home: Path) -> None:
    # A mount can be down while you configure it, so this saves anyway.
    from src.client.cli.main import app

    result = CliRunner().invoke(app, ["config", "map-root", MAC, "/nonexistent/media-01"])
    assert result.exit_code == 0, result.output
    assert "not a folder" in result.output
    assert load_config().root_map == {MAC: "/nonexistent/media-01"}


@pytest.mark.fast
def test_cli_unmap_root(home: Path) -> None:
    from src.client.cli.main import app

    save_config(CLIConfig(root_map={MAC: "/mnt/media-01", "/Volumes/media-02": "/mnt/media-02"}))
    runner = CliRunner()
    result = runner.invoke(app, ["config", "unmap-root", f"{MAC}/"])
    assert result.exit_code == 0, result.output
    assert load_config().root_map == {"/Volumes/media-02": "/mnt/media-02"}

    missing = runner.invoke(app, ["config", "unmap-root", MAC])
    assert missing.exit_code != 0


@pytest.mark.fast
def test_library_list_shows_where_each_root_is_here(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    main = importlib.import_module("src.client.cli.main")
    save_config(CLIConfig(root_map={MAC: "/mnt/media-01"}))

    class FakeResp:
        def json(self) -> list[dict]:
            return [
                {"library_id": "lib_1", "name": "Footage", "root_path": f"{MAC}/Footage"},
                {"library_id": "lib_2", "name": "Local", "root_path": "/srv/photos"},
            ]

    class FakeClient:
        def get(self, url: str, **kw: object) -> FakeResp:
            return FakeResp()

    from rich.console import Console

    monkeypatch.setattr(main, "LumiverbClient", FakeClient)
    monkeypatch.setattr(main, "console", Console(width=200))
    result = CliRunner().invoke(main.app, ["library", "list"])
    assert result.exit_code == 0, result.output
    assert "/mnt/media-01/Footage" in result.output
    # Unmapped roots show nothing extra: the path is already right here.
    assert result.output.count("/srv/photos") == 1


# ---------------------------------------------------------------------------
# Scan and enrich use the mapped root
# ---------------------------------------------------------------------------


def _mapped_library(home: Path, tmp_path: Path) -> tuple[dict, Path]:
    mount = tmp_path / "mnt"
    (mount / "Footage").mkdir(parents=True)
    (mount / "Footage" / "a.mov").write_bytes(b"x")
    save_config(CLIConfig(root_map={MAC: str(mount)}))
    return {"library_id": "lib_1", "name": "Footage", "root_path": f"{MAC}/Footage"}, (mount / "Footage").resolve()


@pytest.mark.fast
def test_scan_walks_the_mapped_root(home: Path, tmp_path: Path) -> None:
    from unittest.mock import MagicMock, patch

    from rich.console import Console

    from src.client.cli.scan import run_scan

    library, here = _mapped_library(home, tmp_path)
    with (
        patch("src.client.cli.scan._load_tenant_filters", return_value=[]),
        patch("src.client.cli.scan._load_library_filters", return_value=[]),
        patch("src.client.cli.scan._walk_library", return_value=[]) as walk,
    ):
        run_scan(MagicMock(), library, console=Console(quiet=True))
    assert walk.call_args.args[0] == here


@pytest.mark.fast
def test_scan_of_an_unreachable_root_touches_nothing(home: Path, tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    from rich.console import Console

    from src.client.cli.scan import run_scan

    save_config(CLIConfig(root_map={MAC: str(tmp_path / "not-mounted")}))
    client = MagicMock()
    stats = run_scan(client, {"library_id": "lib_1", "root_path": f"{MAC}/Footage"}, console=Console(quiet=True))
    assert stats.new == 0 and stats.failed == 0
    client.get.assert_not_called()
    client.post.assert_not_called()
    client.delete.assert_not_called()


@pytest.mark.fast
def test_enrich_probes_files_under_the_mapped_root(home: Path, tmp_path: Path) -> None:
    from unittest.mock import MagicMock, patch

    from rich.console import Console

    from src.client.cli.repair import run_repair

    library, here = _mapped_library(home, tmp_path)
    client = MagicMock()
    asset = {"asset_id": "ast_1", "rel_path": "a.mov"}
    with (
        patch("src.client.cli.repair.get_repair_summary", return_value={"total_assets": 1, "missing_probe": 1}),
        patch("src.client.cli.repair._page_missing", return_value=[asset]),
        patch("src.client.cli.repair._probe_one", return_value="ok") as probe,
    ):
        run_repair(client, library, job_type="probe", console=Console(quiet=True))
    assert probe.call_args.args[1] == here


@pytest.mark.fast
def test_scan_command_says_the_root_is_unreachable_and_fails(home: Path, tmp_path: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    main = importlib.import_module("src.client.cli.main")
    save_config(CLIConfig(root_map={MAC: str(tmp_path / "asleep")}))

    class FakeResp:
        def json(self) -> list[dict]:
            return [{"library_id": "lib_1", "name": "Footage", "root_path": f"{MAC}/Footage"}]

    class FakeClient:
        def get(self, url: str, **kw: object) -> FakeResp:
            return FakeResp()

    monkeypatch.setattr(main, "LumiverbClient", FakeClient)
    result = CliRunner().invoke(main.app, ["scan", "--library", "Footage"])
    assert result.exit_code == 1
    assert "not accessible" in result.output
    assert "Done:" not in result.output


# ---------------------------------------------------------------------------
# A folder fstab says is a mount counts only while it's mounted
# ---------------------------------------------------------------------------


def _fstab(tmp_path: Path, *mount_points: str) -> Path:
    f = tmp_path / "fstab"
    lines = ["# comment", "UUID=abc / ext4 defaults 0 1"]
    lines += [f"//10.10.10.1/share {mp} cifs soft 0 0" for mp in mount_points]
    f.write_text("\n".join(lines) + "\n")
    return f


@pytest.fixture
def mounts(monkeypatch: pytest.MonkeyPatch):
    """The set of paths os.path.ismount says are mounted."""
    mounted: set[str] = set()
    monkeypatch.setattr(roots.os.path, "ismount", lambda p: str(p) in mounted or str(p) == "/")
    return mounted


def test_an_unmounted_mount_point_is_away_even_with_files_in_it(tmp_path: Path, monkeypatch, mounts) -> None:
    # Files written into the mount point while unmounted (a stray copy) look
    # like a library, but aren't the share.
    share = tmp_path / "mnt" / "media-01"
    (share / "Media").mkdir(parents=True)
    (share / "Media" / "stray.mov").write_text("x")
    monkeypatch.setattr(roots, "FSTAB", _fstab(tmp_path, str(share)))
    lib = {"root_path": "/Volumes/media-01/Media"}
    root_map = {"/Volumes/media-01": str(share)}
    assert reachable_root(lib, root_map=root_map, require_entries=True) is None
    mounts.add(str(share))
    assert reachable_root(lib, root_map=root_map, require_entries=True) == (share / "Media").resolve()


def test_folders_fstab_doesnt_mention_are_checked_as_before(tmp_path: Path, monkeypatch, mounts) -> None:
    local = tmp_path / "dev" / "das" / "Journey"
    local.mkdir(parents=True)
    (local / "a.mov").write_text("x")
    monkeypatch.setattr(roots, "FSTAB", _fstab(tmp_path, "/mnt/somewhere-else"))
    lib = {"root_path": "/Volumes/brain-das/Journey"}
    assert reachable_root(lib, root_map={"/Volumes/brain-das": str(tmp_path / "dev" / "das")},
                          require_entries=True) == local.resolve()


def test_the_deepest_fstab_mount_point_is_the_one_checked(tmp_path: Path, monkeypatch, mounts) -> None:
    outer = tmp_path / "mnt"
    inner = outer / "media-02"
    (inner / "Photos").mkdir(parents=True)
    (inner / "Photos" / "p.jpg").write_text("x")
    monkeypatch.setattr(roots, "FSTAB", _fstab(tmp_path, str(outer), str(inner)))
    mounts.add(str(outer))  # the outer disk is there, the share isn't
    lib = {"root_path": "/Volumes/media-02/Photos"}
    assert reachable_root(lib, root_map={"/Volumes/media-02": str(inner)}, require_entries=True) is None


def test_an_unreadable_fstab_doesnt_block_anything(tmp_path: Path, monkeypatch, mounts) -> None:
    local = tmp_path / "lib"
    local.mkdir()
    (local / "a.mov").write_text("x")
    monkeypatch.setattr(roots, "FSTAB", tmp_path / "no-such-fstab")
    assert reachable_root({"root_path": str(local)}, root_map={}, require_entries=True) == local.resolve()


def test_fstab_octal_escapes_are_read(tmp_path: Path, monkeypatch, mounts) -> None:
    # fstab writes a space as \040, a tab as \011, a backslash as \134.
    share = tmp_path / "mnt" / "media\t01"
    (share / "Media").mkdir(parents=True)
    (share / "Media" / "a.mov").write_text("x")
    escaped = str(share).replace("\t", "\\011")
    (tmp_path / "fstab").write_text(f"//nas/share {escaped} cifs soft 0 0\n")
    monkeypatch.setattr(roots, "FSTAB", tmp_path / "fstab")
    lib = {"root_path": str(share / "Media")}
    assert reachable_root(lib, root_map={}, require_entries=True) is None
    mounts.add(str(share))
    assert reachable_root(lib, root_map={}, require_entries=True) == (share / "Media").resolve()


def test_a_root_reached_through_a_symlink_is_checked_where_it_lands(tmp_path: Path, monkeypatch, mounts) -> None:
    share = tmp_path / "mnt" / "media-01"
    (share / "Media").mkdir(parents=True)
    (share / "Media" / "a.mov").write_text("x")
    link = tmp_path / "das"
    link.symlink_to(share)
    monkeypatch.setattr(roots, "FSTAB", _fstab(tmp_path, str(share)))
    lib = {"root_path": "/Volumes/media-01/Media"}
    root_map = {"/Volumes/media-01": str(link)}
    assert reachable_root(lib, root_map=root_map, require_entries=True) is None
    mounts.add(str(share))
    assert reachable_root(lib, root_map=root_map, require_entries=True) == (share / "Media").resolve()
