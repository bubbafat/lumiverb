"""Whether a device holds its remakes back (ADR-018 decision 6): on battery,
or on a metered network (src/processing/power.py), from what a Mac's pmset,
Linux's power supplies and NetworkManager say. What can't be told goes on."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.processing import power

pytestmark = pytest.mark.fast

ON_AC = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1234)\t100%; charged; 0:00 remaining present: true\n"
ON_BATTERY = "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1234)\t80%; discharging; 5:12 remaining\n"
DESKTOP = "Now drawing from 'AC Power'\n"


def test_a_mac_on_battery_says_so():
    assert power.battery_in_pmset(ON_BATTERY)
    assert not power.battery_in_pmset(ON_AC) and not power.battery_in_pmset(DESKTOP)
    assert not power.battery_in_pmset("")  # pmset not there, or failed: goes on


def _supply(root: Path, name: str, kind: str, status: str | None = None) -> None:
    (root / name).mkdir(parents=True)
    (root / name / "type").write_text(kind + "\n")
    if status is not None:
        (root / name / "status").write_text(status + "\n")


def test_linux_on_battery_is_a_battery_discharging(tmp_path: Path):
    _supply(tmp_path, "AC", "Mains")
    _supply(tmp_path, "BAT0", "Battery", "Discharging")
    assert power.battery_in_supplies(tmp_path)
    (tmp_path / "BAT0" / "status").write_text("Charging\n")
    assert not power.battery_in_supplies(tmp_path)
    (tmp_path / "BAT0" / "status").write_text("Full\n")
    assert not power.battery_in_supplies(tmp_path)


def test_a_desktop_or_no_supplies_goes_on(tmp_path: Path):
    _supply(tmp_path, "AC", "Mains")
    assert not power.battery_in_supplies(tmp_path)
    assert not power.battery_in_supplies(tmp_path / "not-there")


def test_a_metered_connection_is_one_networkmanager_says_is():
    wifi = "GENERAL.STATE:100 (connected)\nGENERAL.METERED:yes (guessed)\n"
    wired = "GENERAL.STATE:100 (connected)\nGENERAL.METERED:no\n"
    unused = "GENERAL.STATE:30 (disconnected)\nGENERAL.METERED:yes\n"
    loopback = "GENERAL.STATE:10 (unmanaged)\nGENERAL.METERED:unknown\n"
    assert power.metered_in_nmcli(wifi + loopback)
    assert not power.metered_in_nmcli(wired + unused + loopback)
    assert not power.metered_in_nmcli("")  # no NetworkManager: goes on


def test_held_says_why(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(power, "on_battery", lambda: True)
    monkeypatch.setattr(power, "metered", lambda: True)
    assert power.held() == "battery"
    monkeypatch.setattr(power, "on_battery", lambda: False)
    assert power.held() == "metered"
    monkeypatch.setattr(power, "metered", lambda: False)
    assert power.held() is None


# ---------------------------------------------------------------------------
# A device's remakes: after everything else, yielding to new work
# ---------------------------------------------------------------------------


class _Client:
    def post(self, path, **kw):
        raise AssertionError("nothing is sent")


def _device(held=None, *, every: float = 900.0):
    from src.processing.device import Device

    now = [10_000.0]
    device = Device(_Client(), full_scan_every=every, clock=lambda: now[0], held=lambda: held)
    device.libraries = {"lib_1": {"library_id": "lib_1", "scans_paused": False}}
    device.marks = {"lib_1": 4}
    device.scan_state.last_full_scan["lib_1"] = now[0] - 60  # scanned a minute ago
    return device, now


def test_remakes_go_on_until_new_work_may_have_come_and_always_make_one():
    from src.processing.device import _Remaking

    device, now = _device()
    remaking = _Remaking(device)
    assert not remaking.held() and not remaking.stop() and not remaking.stop()
    device.marks = {"lib_1": 5}  # something changed on the server (the heartbeat's check-in heard it)
    assert remaking.stop() and remaking.stopped

    device, now = _device()
    remaking = _Remaking(device)
    now[0] += 900  # its next full scan is due
    assert not remaking.stop(), "one job each pass, whatever is due"
    assert remaking.stop()

    device, now = _device()
    remaking = _Remaking(device)
    remaking.stop()
    device.libraries["lib_1"]["scan_now"] = True
    assert remaking.stop()
    device, now = _device()
    remaking = _Remaking(device)
    remaking.stop()
    device.libraries["lib_1"].update(scan_now=True, scans_paused=True)  # paused: no scan comes
    now[0] += 900
    assert not remaking.stop()


def test_remakes_wait_at_once_while_held_and_say_why():
    from src.processing.device import _Remaking

    device, _ = _device("battery")
    remaking = _Remaking(device)
    assert remaking.held() and remaking.stop()
    assert device.remakes_held == "battery"  # its next check-in says it
