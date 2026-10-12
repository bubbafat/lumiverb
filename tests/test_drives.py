"""Whether a chosen folder's drive is there (src/server/drives.py), before
Lumiverb writes to it or makes it: an fstab mount point that isn't mounted,
a Mac's /Volumes or a Linux desktop's /media with the drive unplugged, or
right in /. Each would put the previews or backups on the system disk.
Faked here: fstab (roots.FSTAB), what's mounted (os.path.ismount), and where
drives appear."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.processing import roots
from src.server import backups, drives
from src.server.api.errors import ApiError
from src.server.api.new_folder import made_if_asked
from src.server.storage import move

pytestmark = pytest.mark.fast


@pytest.fixture
def mounts(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """The paths os.path.ismount says are mounted."""
    mounted: set[str] = {"/"}
    monkeypatch.setattr(drives.os.path, "ismount", lambda p: str(p) in mounted)
    return mounted


def _fstab(tmp_path: Path, *mounts: Path) -> Path:
    f = tmp_path / "fstab"
    f.write_text("".join(f"UUID=x{i} {m} ext4 defaults,nofail 0 2\n" for i, m in enumerate(mounts)))
    return f


def test_an_fstab_mount_point_not_mounted_has_no_drive(tmp_path, monkeypatch, mounts):
    ssd = tmp_path / "mnt" / "ssd2"
    ssd.mkdir(parents=True)  # the empty mount point, on the system disk
    monkeypatch.setattr(roots, "FSTAB", _fstab(tmp_path, ssd))
    said = f"There's no drive at {ssd}. Is it mounted?"
    assert drives.missing(ssd) == said, "chosen as it is"
    assert drives.missing(ssd / "lumiverb-previews") == said, "a folder to make in it"
    with pytest.raises(ApiError) as refused, made_if_asked(str(ssd / "lumiverb-previews"), True, tmp_path):
        pass
    assert refused.value.message == said and not (ssd / "lumiverb-previews").exists()
    with pytest.raises(move.MoveError, match="Is it mounted"):
        move.check_target(tmp_path / "data", ssd)
    with pytest.raises(backups.BackupError, match="Is it mounted"):
        backups.check_folder(ssd)
    mounts.add(str(ssd))
    assert drives.missing(ssd) is None and drives.missing(ssd / "lumiverb-previews") is None


def test_one_reached_through_a_symlink_too(tmp_path, monkeypatch, mounts):
    ssd = tmp_path / "mnt" / "ssd2"
    ssd.mkdir(parents=True)
    (tmp_path / "fast").symlink_to(ssd)
    monkeypatch.setattr(roots, "FSTAB", _fstab(tmp_path, ssd))
    assert drives.missing(tmp_path / "fast" / "previews") == f"There's no drive at {ssd}. Is it mounted?"


def test_where_a_mac_or_a_linux_desktop_puts_drives_one_not_plugged_in_has_none(tmp_path, monkeypatch, mounts):
    volumes, media, run_media = tmp_path / "Volumes", tmp_path / "media", tmp_path / "run" / "media"
    for folder in (volumes, media / "robert", run_media / "robert"):
        folder.mkdir(parents=True)
    monkeypatch.setattr(drives, "DRIVE_FOLDERS", (volumes, media, run_media))
    monkeypatch.setattr(drives, "USER_DRIVE_FOLDERS", (media, run_media))
    for drive in (volumes / "CACHE", media / "robert" / "CACHE", run_media / "robert" / "CACHE", media / "CACHE"):
        assert drives.missing(drive / "Lumiverb" / "previews") == f"There's no drive at {drive}. Is it plugged in?"
    # Plugged in: its folder is there, and mounted.
    for drive in (volumes / "CACHE", media / "robert" / "CACHE", media / "LABEL"):
        drive.mkdir()
        mounts.add(str(drive))
        assert drives.missing(drive / "Lumiverb" / "previews") is None


def test_a_linux_desktop_user_with_no_drive_yet_is_said_by_the_drive(tmp_path, monkeypatch, mounts):
    import getpass

    media = tmp_path / "media"
    media.mkdir()  # udisks makes /media/<user> only when a drive is first mounted
    monkeypatch.setattr(drives, "DRIVE_FOLDERS", (media,))
    monkeypatch.setattr(drives, "USER_DRIVE_FOLDERS", (media,))
    user = getpass.getuser()
    assert drives.missing(media / user / "USB" / "previews") == f"There's no drive at {media / user / 'USB'}. Is it plugged in?"
    # Not a user: a drive mounted right in /media.
    assert drives.missing(media / "LumiverbNoSuchUserUSB" / "previews") == (
        f"There's no drive at {media / 'LumiverbNoSuchUserUSB'}. Is it plugged in?")


def test_nothing_is_made_right_in_the_root(mounts):
    assert drives.missing(Path("/lumiverb-no-such-drive/previews")) == (
        "There's no drive at /lumiverb-no-such-drive. Is it plugged in?")


def test_an_ordinary_folder_or_one_to_make_in_it_is_fine(tmp_path, mounts):
    assert drives.missing(tmp_path) is None
    assert drives.missing(tmp_path / "Lumiverb" / "previews") is None
