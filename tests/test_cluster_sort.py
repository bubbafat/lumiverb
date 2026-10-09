"""The order of unnamed clusters (GET /v1/faces/clusters?sort=...).

Largest first by default, so naming makes the most progress fastest. Ties
go by the cluster's first face id, which stays put while its faces do, so
the order doesn't shuffle between loads (the cluster_index is a position in
one computation and does).
"""

from __future__ import annotations

import pytest

from src.server.api.routers.people import CLUSTER_SORTS, order_clusters


def _c(index: int, size: int, first_face: str, newest: str | None = None) -> dict:
    return {"cluster_index": index, "size": size, "face_ids": [f"face_{first_face}", "face_zzz"][:size],
            "newest": newest}


CLUSTERS = [
    _c(0, 3, "b", "2024-05-01T00:00:00+00:00"),
    _c(1, 9, "x", "2021-01-01T00:00:00+00:00"),
    _c(2, 3, "a", None),
    _c(3, 5, "c", "2025-02-02T00:00:00+00:00"),
    _c(4, 5, "d", "2025-02-02T00:00:00+00:00"),
]


def _indices(clusters: list[dict]) -> list[int]:
    return [c["cluster_index"] for c in clusters]


@pytest.mark.fast
def test_sorts_offered() -> None:
    assert CLUSTER_SORTS == ("size_desc", "size_asc", "newest")


@pytest.mark.fast
def test_largest_first_ties_by_first_face() -> None:
    assert _indices(order_clusters(CLUSTERS, "size_desc")) == [1, 3, 4, 2, 0]


@pytest.mark.fast
def test_smallest_first_ties_by_first_face() -> None:
    assert _indices(order_clusters(CLUSTERS, "size_asc")) == [2, 0, 3, 4, 1]


@pytest.mark.fast
def test_newest_photo_first_undated_last() -> None:
    assert _indices(order_clusters(CLUSTERS, "newest")) == [3, 4, 0, 1, 2]


@pytest.mark.fast
def test_order_does_not_depend_on_the_order_given() -> None:
    for sort in CLUSTER_SORTS:
        assert order_clusters(CLUSTERS, sort) == order_clusters(list(reversed(CLUSTERS)), sort)


@pytest.mark.fast
def test_a_cache_from_before_newest_still_sorts() -> None:
    old = [{k: v for k, v in c.items() if k != "newest"} for c in CLUSTERS]
    assert _indices(order_clusters(old, "newest")) == [2, 0, 3, 4, 1]
