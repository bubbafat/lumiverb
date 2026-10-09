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


def test_no_embedding_pairs_rank_after_embedding_pairs() -> None:
    """An old face without an embedding overlaps both new detections more
    than the old face with one does; the embedding pair is decided first."""
    old = [Row("plain", _box(0.30), None, True), Row("known", _box(0.34), _text(PEOPLE[0]), True)]
    new = [{"bounding_box": _box(0.32), "embedding": _seen_again(PEOPLE[0])}]

    assert FaceRepository._pair_redetected_faces(old, new) == {0: "known"}


@pytest.mark.parametrize(("distance", "paired"), [(0.35, True), (0.42, False)])
def test_one_gate_for_overlap_and_embedding_alone(distance: float, paired: bool) -> None:
    """The same 0.4 gate applies whether or not the boxes overlap."""
    import math

    angle = math.acos(1 - distance)
    seen = _vec((0, math.cos(angle)), (7, math.sin(angle)))
    for new_box in (_box(0.32), _box(0.75)):  # overlapping, then far away
        old = [Row("f0", _box(0.30), _text(PEOPLE[0]), True)]
        new = [{"bounding_box": new_box, "embedding": seen}]
        assert (FaceRepository._pair_redetected_faces(old, new) == {0: "f0"}) is paired


SwitchRow = namedtuple("SwitchRow", "face_id bounding_box_json emb confirmed embedding_model")


@pytest.mark.parametrize(("x", "paired"), [(0.36, True), (0.40, False), (0.45, False)])
def test_across_a_face_model_switch_only_a_substantial_overlap_pairs(x: float, paired: bool) -> None:
    """Robert, Oct 9: names carry over a model switch only where the boxes
    overlap substantially (IoU 0.5 or more), not where they just touch,
    even with one face before and one now."""
    old = [SwitchRow("f0", _box(0.30), _text(PEOPLE[0]), True, "buffalo_l")]
    new = [{"bounding_box": _box(x), "embedding": _vec((3, 1.0))}]
    # IoU at x=0.36: 0.14/0.26 = 0.54; at 0.40: 0.10/0.30 = 0.33; at 0.45: 0.05/0.35 = 0.14.
    assert (FaceRepository._pair_redetected_faces(old, new, "antelopev2") == {0: "f0"}) is paired


def test_without_a_switch_boxes_without_embeddings_pair_from_0_3() -> None:
    old = [SwitchRow("f0", _box(0.30), None, True, "buffalo_l")]
    new = [{"bounding_box": _box(0.40)}]  # IoU 0.33
    assert FaceRepository._pair_redetected_faces(old, new, "buffalo_l") == {0: "f0"}


def test_across_a_switch_a_face_without_an_embedding_needs_a_substantial_overlap_too():
    # Review: a named face never embedded (too small, say) paired at 0.3 across a switch.
    old = [SwitchRow("f0", _box(0.30), None, True, "buffalo_l")]
    assert FaceRepository._pair_redetected_faces(old, [{"bounding_box": _box(0.40)}], "antelopev2") == {}  # IoU 0.33
    assert FaceRepository._pair_redetected_faces(old, [{"bounding_box": _box(0.36)}], "antelopev2") == {0: "f0"}
