"""Re-detection pairing (FaceRepository._pair_redetected_faces), without a DB.

A re-found face keeps its id, and with it every human decision attached to
it, so pairing must never hand a face's id to a different person.
"""

from __future__ import annotations

from collections import namedtuple

import pytest

from src.server.repository.tenant import FaceRepository

pytestmark = pytest.mark.fast

Row = namedtuple("Row", "face_id bounding_box_json emb confirmed")


def _vec(*weights: tuple[int, float]) -> list[float]:
    v = [0.0] * 512
    for i, w in weights:
        v[i] = w
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v]


def _text(v: list[float]) -> str:
    return "[" + ",".join(str(x) for x in v) + "]"


def _box(x: float) -> dict:
    return {"x": x, "y": 0.1, "w": 0.2, "h": 0.2}


# Three people in a row; neighbours look somewhat alike (distance 0.5).
PEOPLE = [_vec((0, 1.0)), _vec((0, 0.5), (1, 0.75 ** 0.5)), _vec((1, 0.5), (2, 0.75 ** 0.5))]


def _seen_again(v: list[float]) -> list[float]:
    return _vec(*((i, x) for i, x in enumerate(v) if x), (300, 0.05))


def test_three_people_in_a_row_keep_their_faces() -> None:
    old = [Row(f"f{i}", _box(0.30 + 0.1 * i), _text(PEOPLE[i]), True) for i in range(3)]
    new = [{"bounding_box": _box(0.36 + 0.1 * i), "embedding": _seen_again(PEOPLE[i])} for i in range(3)]

    assert FaceRepository._pair_redetected_faces(old, new) == {0: "f0", 1: "f1", 2: "f2"}


def test_input_order_does_not_matter() -> None:
    old = [Row(f"f{i}", _box(0.30 + 0.1 * i), _text(PEOPLE[i]), True) for i in range(3)]
    new = [{"bounding_box": _box(0.36 + 0.1 * i), "embedding": _seen_again(PEOPLE[i])} for i in (2, 0, 1)]

    assert FaceRepository._pair_redetected_faces(old, new) == {0: "f2", 1: "f0", 2: "f1"}


def test_unrelated_face_in_the_same_place_is_new() -> None:
    old = [Row("f0", _box(0.30), _text(PEOPLE[0]), True)]
    new = [{"bounding_box": _box(0.30), "embedding": _vec((9, 1.0))}]

    assert FaceRepository._pair_redetected_faces(old, new) == {}
