"""Face detection's levels, one for photos and one for videos (Settings ›
Processing: Off / Low / Normal / High). Pure: no media libraries.

Off finds no new faces, in neither new clips nor old: the producer applies
to nothing while it's off (ON), so nothing is missing and nothing waits.
The faces already found, and their names, stay.

A photo's level says which faces count (GATES): Low only clear ones
(large, sharp, confident), Normal what was always found, High smaller and
softer ones too. A video's says which frames are looked at
(src/producers/video_faces/frames.py), each with Normal's gates.

Measured on the repo's face fixtures with buffalo_l: the crowd photo gives
0 faces at Low, 2 at Normal and about 11 at High; the portrait's one face
(confidence 0.83, 299 px wide) is found at each.
"""

from __future__ import annotations

# Lowest first: lineage records a level as its index here (made below the
# setting: out of date; at or above: current), so Off, made nowhere, is 0.
DETAILS: tuple[str, ...] = ("off", "low", "normal", "high")
OFF = "off"

GATES: dict[str, dict[str, float]] = {
    "low": {"min_confidence": 0.7, "min_area_fraction": 0.01, "min_face_pixels": 80,
            "min_relative_size": 0.3, "min_sharpness": 30.0},
    # What faces were found with before there were levels (insightface_provider's defaults).
    "normal": {"min_confidence": 0.5, "min_area_fraction": 0.003, "min_face_pixels": 40,
               "min_relative_size": 0.15, "min_sharpness": 15.0},
    # Not under 0.4: under 0.3 the detector hands back so many boxes that detection slows a lot.
    "high": {"min_confidence": 0.4, "min_area_fraction": 0.0005, "min_face_pixels": 20,
             "min_relative_size": 0.05, "min_sharpness": 8.0},
}


def gates(detail: str | None) -> dict[str, float]:
    """The gates a photo's faces are held to at a level (Normal's for one it doesn't know, or Off)."""
    return dict(GATES.get(detail or "normal", GATES["normal"]))


def on(artifact: str, default: str) -> str:
    """SQL: the producer's level, as the account has it, isn't Off. In its
    applies, so it applies to nothing while it's off."""
    return (f"COALESCE((SELECT CAST(sm.value AS jsonb) ->> 'detail' FROM system_metadata sm"
            f" WHERE sm.key = 'producer.{artifact}'), '{default}') <> '{OFF}'")
