"""The location producer's math (src/producers/location/infer.py): gaps
between clips across zones and clocks, a device's clock offset from scene
pairs, the radius and the speed check. ADR-017 phase 3."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from src.producers.location.infer import (
    NO_CLOCK,
    Clock,
    Fix,
    Stamp,
    between,
    both_sides_radius_m,
    clock_offset,
    distance_m,
    gap_minutes,
    locate,
    one_side_radius_m,
)
from src.producers.location.work import day_clock

pytestmark = pytest.mark.fast

UTC = timezone.utc  # noqa: UP017 — as the rest of the tests say it
PHONE = "Apple|iPhone 15 Pro|"
CAMERA = "Blackmagic Design|Pocket Cinema Camera 6K|1234"


def wall(text: str) -> datetime:
    """A wall clock as stored: as if UTC."""
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def at(text: str, offset: int | None = None, device: str = CAMERA) -> Stamp:
    return Stamp(wall(text), offset, device)


def fix(asset_id: str, text: str, lat: float, lon: float, offset: int | None = None, device: str = PHONE,
        accuracy: float | None = None, label: str = "iPhone 15 Pro photos") -> Fix:
    return Fix(asset_id, at(text, offset, device), lat, lon, accuracy, label)


# ---------------------------------------------------------------------------
# Gaps: zones, wall clocks, devices, daylight saving
# ---------------------------------------------------------------------------


def test_with_both_zones_the_gap_is_between_real_instants() -> None:
    # 14:00 in Paris (+02:00) and 08:00 in New York (-04:00) are the same instant.
    assert gap_minutes(at("2024-06-15T14:00", 120), at("2024-06-15T08:00", -240, PHONE)) == 0
    assert gap_minutes(at("2024-06-15T14:30", 120), at("2024-06-15T08:00", -240, PHONE)) == 30


def test_without_a_zone_the_wall_clocks_are_compared_as_written() -> None:
    """A camera with no zone is taken to be on the local time of the fix
    (the ADR's offset 0): its wall clock against the phone's."""
    assert gap_minutes(at("2024-06-15T14:00"), at("2024-06-15T14:05", 120, PHONE)) == -5
    assert gap_minutes(at("2024-06-15T14:00", 120), at("2024-06-15T13:00", None, PHONE)) == 60


def test_the_clock_offset_applies_to_other_devices_only() -> None:
    clock = Clock(60.0, 4)
    other, same = at("2024-06-15T13:00", None, PHONE), at("2024-06-15T13:00", None, CAMERA)
    assert gap_minutes(at("2024-06-15T14:00"), other, clock) == 0
    assert gap_minutes(at("2024-06-15T14:00"), same, clock) == 60


def test_falling_back_an_hour_doesnt_reorder_fixes() -> None:
    """New York, 3 Nov 2024: 01:50 EDT comes 20 minutes before 01:10 EST.
    With zones on both sides the later wall clock isn't taken as later."""
    edt = fix("edt", "2024-11-03T01:50", 40.70, -74.0, offset=-240)  # 05:50Z
    est = fix("est", "2024-11-03T01:10", 40.80, -74.0, offset=-300)  # 06:10Z
    clip = at("2024-11-03T01:20", -300)  # 06:20Z
    assert gap_minutes(clip, edt.stamp) == 30 and gap_minutes(clip, est.stamp) == 10
    found = locate(clip, [edt, est], 360)["guess"]
    assert found["basis"]["method"] == "nearest"
    assert [f["asset_id"] for f in found["basis"]["fixes"]] == ["est"]
    assert found["radius_m"] == round(one_side_radius_m(10))


def test_springing_forward_an_hour_interpolates_by_the_real_minutes() -> None:
    """New York, 10 Mar 2024: 01:55 EST and 03:05 EDT are 10 minutes apart, not 70."""
    a = fix("a", "2024-03-10T01:55", 40.0, -74.0, offset=-300)  # 06:55Z
    b = fix("b", "2024-03-10T03:05", 40.1, -74.0, offset=-240)  # 07:05Z
    found = locate(at("2024-03-10T03:00", -240), [a, b], 360)["guess"]  # 07:00Z: halfway
    assert found["basis"]["method"] == "between"
    assert found["lat"] == pytest.approx(40.05, abs=1e-6)
    assert [f["gap_min"] for f in found["basis"]["fixes"]] == [5.0, -5.0]


def test_a_zone_unknown_on_one_side_compares_wall_clocks_even_across_dst() -> None:
    # A camera with no zone at 01:20 against the fall-back fixes: by the walls,
    # 01:10 is 10 minutes before it and 01:50 30 minutes after.
    edt = fix("edt", "2024-11-03T01:50", 40.70, -74.0, offset=-240)
    est = fix("est", "2024-11-03T01:10", 40.80, -74.0, offset=-300)
    found = locate(at("2024-11-03T01:20"), [edt, est], 360)["guess"]
    assert found["basis"]["method"] == "between"
    assert [f["asset_id"] for f in found["basis"]["fixes"]] == ["est", "edt"]


# ---------------------------------------------------------------------------
# Clock offset from scene pairs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("votes,want", [
    ([61, 59, 60], Clock(60.0, 3)),
    ([60, 62], NO_CLOCK),  # two aren't enough
    ([60, 75, 90], NO_CLOCK),  # three, but not within 10 minutes
    ([-120, 60, 61, 62, 63, 400], Clock(61.5, 4)),  # outliers left out
    ([0, 1, 2, 300, 301, 302], Clock(1.0, 3)),  # equal groups: the one nearest no offset
    ([float("nan"), 5, 6, 7], Clock(6.0, 3)),
    ([], NO_CLOCK),
])
def test_the_clock_offset_is_the_median_of_three_agreeing_within_ten_minutes(votes, want) -> None:
    assert clock_offset(votes) == want


def test_scene_pairs_vote_and_weak_ones_dont() -> None:
    pairs = [{"camera_taken_at": f"2024-06-15T1{i}:00+00:00", "camera_offset_min": None,
              "fix_taken_at": f"2024-06-15T0{i}:00+00:00", "fix_offset_min": 120, "fix_device": PHONE,
              "similarity": s} for i, s in ((1, 0.95), (2, 0.93), (3, 0.97), (4, 0.5))]
    # The camera's clock reads 10 hours ahead of the phone's wall clock.
    assert day_clock({"device": CAMERA, "pairs": pairs}) == Clock(600.0, 3)
    assert day_clock({"device": CAMERA, "pairs": pairs[:2] + pairs[3:]}) == NO_CLOCK


def test_a_corrected_clock_finds_the_fixes_the_written_one_misses() -> None:
    """A camera left on home time, 6 hours ahead: as written, the phone's
    photos are 6 hours away; corrected, they're either side."""
    a = fix("a", "2024-06-15T08:00", 10.0, 20.0)
    b = fix("b", "2024-06-15T09:00", 10.0, 20.1)
    clip = at("2024-06-15T14:30")
    assert locate(clip, [a, b], 60)["guess"] is None
    found = locate(clip, [a, b], 60, Clock(360.0, 5))["guess"]
    assert found["basis"]["method"] == "between" and found["lon"] == pytest.approx(20.05, abs=1e-4)
    assert found["basis"]["clock_offset_min"] == 360.0 and found["basis"]["votes"] == 5
    assert found["basis"]["summary"] == "From iPhone 15 Pro photos 08:00–09:00 · clock 6.0 h ahead"


# ---------------------------------------------------------------------------
# Radius, speed, the window
# ---------------------------------------------------------------------------


def test_distances_and_points_between() -> None:
    assert distance_m(48.8566, 2.3522, 51.5074, -0.1278) == pytest.approx(343_500, rel=0.01)
    assert between(0, 0, 0, 90, 0.5) == pytest.approx((0, 45))
    # Across the antimeridian the short way, not through Greenwich.
    lat, lon = between(0, 179, 0, -179, 0.5)
    assert lat == pytest.approx(0, abs=1e-9) and abs(lon) == pytest.approx(180)
    assert between(10, 10, 10, 10, 0.3) == pytest.approx((10, 10))


def test_the_radius_grows_with_the_gap() -> None:
    assert one_side_radius_m(0) == 100
    assert one_side_radius_m(300) == pytest.approx(20_100)  # 5 hours: "About 20 km"
    assert one_side_radius_m(-300) == one_side_radius_m(300)
    radii = [one_side_radius_m(m) for m in range(0, 361, 30)]
    assert radii == sorted(radii) and len(set(radii)) == len(radii)
    assert both_sides_radius_m(0, 0, 0, 0) == 100
    assert both_sides_radius_m(0, 0, 0, 0.02) == pytest.approx(distance_m(0, 0, 0, 0.02) / 2 + 100)


def test_both_sides_interpolate_with_half_their_distance() -> None:
    a = fix("a", "2024-06-15T14:00", 48.0, 2.0, accuracy=5.0)
    b = fix("b", "2024-06-15T15:00", 48.0, 2.1, accuracy=12.0)
    found = locate(at("2024-06-15T14:15"), [b, a], 360)["guess"]
    assert found["lon"] == pytest.approx(2.025, abs=1e-4) and found["lat"] == pytest.approx(48.0, abs=1e-3)
    # Half the distance + 100 m, + the least precise fix's accuracy.
    assert found["radius_m"] == round(distance_m(48.0, 2.0, 48.0, 2.1) / 2 + 100 + 12)
    assert found["basis"]["summary"] == "From iPhone 15 Pro photos 14:00–15:00"


def test_one_side_copies_the_fix_and_widens_with_the_gap() -> None:
    a = fix("a", "2024-06-15T09:00", 35.0, 139.0, label="Pixel 8 photos")
    found = locate(at("2024-06-15T14:00"), [a], 360)["guess"]
    assert (found["lat"], found["lon"]) == (35.0, 139.0)
    assert found["radius_m"] == 20_100
    assert found["basis"]["summary"] == "From Pixel 8 photos at 09:00"
    after = locate(at("2024-06-15T04:00"), [a], 360)["guess"]
    assert after["radius_m"] == 20_100 and after["basis"]["fixes"] == [{"asset_id": "a", "gap_min": -300.0}]


def test_a_fix_at_the_same_minute_is_copied_with_the_least_radius() -> None:
    a = fix("a", "2024-06-15T14:00", 35.0, 139.0)
    b = fix("b", "2024-06-15T14:30", 36.0, 139.0)
    found = locate(at("2024-06-15T14:00"), [a, b], 360)["guess"]
    assert found["basis"]["method"] == "nearest" and found["radius_m"] == 100


def test_faster_than_900_kmh_between_the_fixes_is_no_guess() -> None:
    # Paris to New York in an hour: a clock or data error.
    paris = fix("p", "2024-06-15T14:00", 48.8566, 2.3522)
    nyc = fix("n", "2024-06-15T15:00", 40.7128, -74.0060)
    result = locate(at("2024-06-15T14:30"), [paris, nyc], 360)
    assert result["guess"] is None and "km/h" in result["why"]
    # The same flight over eight hours is a plane: allowed.
    slow = fix("n", "2024-06-15T22:00", 40.7128, -74.0060)
    assert locate(at("2024-06-15T14:30"), [paris, slow], 720)["guess"] is not None


def test_only_fixes_within_the_window_count() -> None:
    a = fix("a", "2024-06-15T08:00", 1.0, 1.0)
    assert locate(at("2024-06-15T14:00"), [a], 360)["guess"] is not None  # exactly 360: in
    result = locate(at("2024-06-15T14:01"), [a], 360)
    assert result["guess"] is None and result["why"] == "No fix within 360 min"
    assert locate(at("2024-06-15T14:00"), [], 360)["guess"] is None


def test_the_nearest_fix_each_side_is_used() -> None:
    fixes = [fix(n, f"2024-06-15T{h}", 1.0, lon) for n, h, lon in
             (("far", "10:00", 1.0), ("near", "13:50", 1.1), ("next", "14:20", 1.2), ("later", "18:00", 9.0))]
    found = locate(at("2024-06-15T14:00"), fixes, 360)["guess"]
    assert [f["asset_id"] for f in found["basis"]["fixes"]] == ["near", "next"]
    assert math.isclose(found["lon"], 1.1 + 0.1 * 10 / 30, abs_tol=1e-4)


def test_every_minute_of_a_day_is_judged_the_same_way_whatever_the_dates() -> None:
    """Across midnight and month ends: only the minutes between matter."""
    base = wall("2024-02-29T23:30")
    a = Fix("a", Stamp(base, 60, PHONE), 1.0, 1.0)
    b = Fix("b", Stamp(base + timedelta(minutes=60), 60, PHONE), 1.0, 2.0)
    found = locate(Stamp(base + timedelta(minutes=45), 60, CAMERA), [a, b], 360)["guess"]
    assert found["lon"] == pytest.approx(1.75, abs=1e-3)
