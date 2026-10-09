"""Unit tests for the face clustering helper.

Exercises ``_cluster_face_embeddings`` with synthetic embeddings — no DB,
no AI, no real face crops. Lives at the helper level (not the
``FaceRepository`` level) on purpose: the clustering math is what was
broken, and isolating it lets these tests run under ``-m fast``.

The headline regression these tests guard against: the previous
single-linkage union-find implementation collapsed thousands of distinct
identities into one cluster via transitive chaining. HDBSCAN's
density-core requirement should make that impossible.
"""

from __future__ import annotations

import numpy as np
import pytest

from src.producers import registry
from src.server.repository.tenant import (
    LARGE_INPUT_THRESHOLD,
    _cluster_face_embeddings,
)

DEFAULT_MERGE_DISTANCE = registry()["faces"].setting("merge_distance").default


def _unit(v: np.ndarray) -> np.ndarray:
    """Row-wise L2-normalize."""
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def _blob(rng: np.random.Generator, center: np.ndarray, n: int, jitter: float = 0.05) -> np.ndarray:
    """N noisy unit vectors clustered around ``center``.

    Uses 32-d embeddings (real ArcFace is 512-d) — clustering behavior is
    dimension-independent for our purposes and 32-d keeps tests fast.
    """
    noise = rng.normal(scale=jitter, size=(n, center.shape[0]))
    return _unit(center[None, :] + noise).astype(np.float32)


@pytest.mark.fast
def test_two_well_separated_identities() -> None:
    """Two clean identity blobs should produce exactly two clusters."""
    rng = np.random.default_rng(0)
    a_center = np.zeros(32); a_center[0] = 1.0
    b_center = np.zeros(32); b_center[16] = 1.0  # orthogonal to A

    vecs = np.vstack([_blob(rng, a_center, 10), _blob(rng, b_center, 10)])
    clusters = _cluster_face_embeddings(vecs, min_cluster_size=3)

    assert len(clusters) == 2
    sizes = sorted(len(c) for c in clusters)
    assert sizes == [10, 10]
    # Each cluster must be drawn entirely from one of the two source blobs.
    for c in clusters:
        from_a = sum(1 for i in c if i < 10)
        from_b = sum(1 for i in c if i >= 10)
        assert from_a == 0 or from_b == 0, f"cluster mixes identities: {c}"


@pytest.mark.fast
def test_chaining_does_not_merge_distinct_identities() -> None:
    """Regression test for the union-find chaining bug.

    Build two well-separated identity blobs plus a single 'bridge' point
    placed between them. Single-linkage union-find would happily merge the
    two blobs through the bridge; HDBSCAN's density-core requirement means
    one bridge cannot anchor a merge — it gets labeled noise instead.
    """
    rng = np.random.default_rng(1)
    a_center = np.zeros(32); a_center[0] = 1.0
    b_center = np.zeros(32); b_center[1] = 1.0  # near-orthogonal but reachable

    a = _blob(rng, a_center, 10, jitter=0.03)
    b = _blob(rng, b_center, 10, jitter=0.03)
    # Bridge: midpoint, normalized — sits roughly equidistant from both blobs.
    bridge = _unit((a_center + b_center)[None, :]).astype(np.float32)

    vecs = np.vstack([a, b, bridge])
    clusters = _cluster_face_embeddings(vecs, min_cluster_size=3)

    # Must NOT collapse into one giant cluster.
    assert len(clusters) >= 2, f"chaining bug: got {len(clusters)} cluster(s)"
    # No cluster may contain points from both source blobs.
    for c in clusters:
        from_a = sum(1 for i in c if i < 10)
        from_b = sum(1 for i in c if 10 <= i < 20)
        assert from_a == 0 or from_b == 0, (
            f"identities merged via bridge: cluster has {from_a} A + {from_b} B"
        )


@pytest.mark.fast
def test_min_cluster_size_excludes_pairs() -> None:
    """A pair of close points alone is not a cluster.

    The new default ``min_cluster_size=3`` rejects size-2 'clusters'. We
    don't pin the exact membership of the surviving big cluster — HDBSCAN's
    'eom' selection may surface the densest sub-core rather than every blob
    point — only that the pair never produces output.
    """
    rng = np.random.default_rng(2)
    big_center = np.zeros(32); big_center[0] = 1.0
    pair_center = np.zeros(32); pair_center[5] = 1.0

    vecs = np.vstack([
        _blob(rng, big_center, 10),
        _blob(rng, pair_center, 2),  # only two — should not survive
    ])
    clusters = _cluster_face_embeddings(vecs, min_cluster_size=3)

    # The pair indices (10, 11) must not appear in any cluster.
    pair_indices = {10, 11}
    for c in clusters:
        assert not (set(c) & pair_indices), f"pair leaked into cluster: {c}"


@pytest.mark.fast
def test_isolated_noise_points_excluded() -> None:
    """Genuinely isolated points should land in HDBSCAN's noise bucket."""
    rng = np.random.default_rng(3)
    center = np.zeros(32); center[0] = 1.0

    blob = _blob(rng, center, 10)
    # Three isolated noise vectors pointing in random directions.
    raw_noise = rng.normal(size=(3, 32))
    noise = _unit(raw_noise).astype(np.float32)

    vecs = np.vstack([blob, noise])
    clusters = _cluster_face_embeddings(vecs, min_cluster_size=3)

    # Exactly one real cluster, and it must not contain any noise indices.
    assert len(clusters) == 1
    assert all(i < 10 for i in clusters[0])


@pytest.mark.fast
def test_below_min_cluster_size_returns_empty() -> None:
    """Fewer input vectors than min_cluster_size short-circuits to empty."""
    vecs = np.eye(32, dtype=np.float32)[:2]  # only 2 inputs
    assert _cluster_face_embeddings(vecs, min_cluster_size=3) == []


@pytest.mark.fast
def test_clusters_sorted_by_size_desc() -> None:
    """Result ordering contract: clusters[0] is the largest."""
    rng = np.random.default_rng(4)
    big = np.zeros(32); big[0] = 1.0
    med = np.zeros(32); med[10] = 1.0
    small = np.zeros(32); small[20] = 1.0

    vecs = np.vstack([
        _blob(rng, big, 12),
        _blob(rng, med, 7),
        _blob(rng, small, 4),
    ])
    clusters = _cluster_face_embeddings(vecs, min_cluster_size=3)

    sizes = [len(c) for c in clusters]
    assert sizes == sorted(sizes, reverse=True)
    assert sizes == [12, 7, 4]


# ---------------------------------------------------------------------------
# Large-input regime (leaf-selection path)
#
# These exercise the LARGE_INPUT_THRESHOLD branch, which is what fixes the
# "everything in one giant cluster" failure observed on a production library
# after most people had been named: the EOM+single-cluster path returned a
# 501-face mega-cluster spanning many distinct identities because no
# sub-cluster had enough excess of mass to beat the root.
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_large_heterogeneous_input_does_not_collapse_to_one_cluster() -> None:
    """The regression that motivated LARGE_INPUT_THRESHOLD.

    Build a large pool (above the threshold) of many small distinct
    identities. The pre-fix EOM+allow_single_cluster path would have
    returned a single mega-cluster — a faithful synthetic mirror of the
    501-face residue we hit in production. After the fix, leaf selection
    must produce multiple clusters and none of them may span all
    identities.
    """
    rng = np.random.default_rng(5)
    n_identities = 12
    per_identity = 6
    assert n_identities * per_identity > LARGE_INPUT_THRESHOLD, (
        "test setup must exceed the large-input threshold"
    )

    blobs = []
    identity_of_index: list[int] = []
    for k in range(n_identities):
        center = np.zeros(32)
        center[k] = 1.0  # mutually orthogonal centers
        blobs.append(_blob(rng, center, per_identity, jitter=0.04))
        identity_of_index.extend([k] * per_identity)

    vecs = np.vstack(blobs)
    clusters = _cluster_face_embeddings(vecs, min_cluster_size=3)

    assert len(clusters) >= 2, (
        f"single-cluster collapse regression: got {len(clusters)} clusters "
        f"for {len(vecs)} faces from {n_identities} identities"
    )
    # No cluster may span more than one source identity.
    for c in clusters:
        identities = {identity_of_index[i] for i in c}
        assert len(identities) == 1, (
            f"leaf selection merged identities {identities} into one cluster"
        )


@pytest.mark.fast
def test_small_input_path_still_uses_eom_single_cluster() -> None:
    """Below the threshold, the original tiny-library path is preserved.

    A small library with one person of ~10 faces should still surface
    *something* — the legitimate use case for ``allow_single_cluster=True``.
    We don't pin the exact size of the surviving cluster (EOM may pick
    the densest sub-core rather than every blob member, same caveat as
    ``test_min_cluster_size_excludes_pairs``), only that we get a single
    non-empty cluster instead of the whole input being labeled noise.
    """
    rng = np.random.default_rng(7)
    center = np.zeros(32); center[0] = 1.0
    n = 10  # well below LARGE_INPUT_THRESHOLD

    vecs = _blob(rng, center, n, jitter=0.03)
    clusters = _cluster_face_embeddings(vecs, min_cluster_size=3)

    assert len(clusters) == 1
    assert len(clusters[0]) >= min(3, n)


@pytest.mark.fast
def test_large_uniform_input_falls_back_to_single_cluster() -> None:
    """The fallback path inside the large-input regime.

    If HDBSCAN's leaf selection finds no leaf cluster meeting
    ``min_cluster_size`` (which happens when the input is one tight
    density mode with no sub-structure), the helper retries with the
    EOM + single-cluster path so the user still sees their one big
    cluster instead of an empty cluster review view.
    """
    rng = np.random.default_rng(8)
    center = np.zeros(32); center[0] = 1.0
    n = LARGE_INPUT_THRESHOLD + 30  # large enough to take the leaf-first path

    vecs = _blob(rng, center, n, jitter=0.03)
    clusters = _cluster_face_embeddings(vecs, min_cluster_size=3)

    # Expect at least one surviving cluster — fallback kicked in.
    assert clusters, "fallback to EOM+single failed: no clusters surfaced"
    # And the surviving cluster(s) must come entirely from the real blob.
    for c in clusters:
        assert all(0 <= i < n for i in c)


# ---------------------------------------------------------------------------
# Merging close clusters (leaf selection + an epsilon)
#
# Real photos of one person don't form one tight blob: years, glasses,
# lighting and angle make a few tight sub-modes. Leaf selection picks the
# finest clusters, so it hands back one group per sub-mode: one person in
# many groups of 5-15. Merging clusters closer than an epsilon puts them
# back together, while distinct people (relatives too) stay apart.
# ---------------------------------------------------------------------------


def _mix(rng: np.random.Generator, base: np.ndarray, cos: float) -> np.ndarray:
    """A unit vector at about ``cos`` to ``base`` (512-d: random directions are near-orthogonal)."""
    other = rng.normal(size=base.shape[0])
    other /= np.linalg.norm(other)
    v = np.sqrt(cos) * base + np.sqrt(1 - cos) * other
    return v / np.linalg.norm(v)


def _photo_library(seed: int, *, sibling_cos: float = 0.55) -> tuple[np.ndarray, np.ndarray]:
    """ArcFace-like embeddings of a family photo library (512-d).

    Eight families of one to four people (relatives share a family
    direction: ``sibling_cos`` between their centers); each person in two
    to five sub-modes, 4-12 faces each; plus 150 strangers seen once.
    Calibrated to InsightFace buffalo_l on LFW (4,268 faces, 212 people):
    there one person's faces are 0.20 / 0.32 / 0.48 apart in cosine
    distance (5th / 50th / 95th percentile), two people's never under
    0.72; here 0.24 / 0.36 / 0.43, and relatives come closer (0.58).
    Labels: the person, -1 for strangers.
    """
    rng = np.random.default_rng(seed)
    dim = 512
    everyone = rng.normal(size=dim)
    everyone /= np.linalg.norm(everyone)
    faces, labels, person = [], [], 0
    for _ in range(8):
        family = _mix(rng, everyone, 0.11)
        for _ in range(rng.integers(1, 5)):
            center = _mix(rng, family, sibling_cos)
            for _ in range(rng.integers(2, 6)):
                mode = _mix(rng, center, 0.85)
                for _ in range(rng.integers(4, 13)):
                    faces.append(_mix(rng, mode, rng.uniform(0.65, 0.8)))
                    labels.append(person)
            person += 1
    for _ in range(150):
        faces.append(_mix(rng, everyone, 0.05))
        labels.append(-1)
    return np.array(faces, dtype=np.float32), np.array(labels)


def _groups_per_person(clusters: list[list[int]], labels: np.ndarray) -> float:
    people = sorted(set(labels[labels >= 0]))
    return float(np.mean([sum(1 for c in clusters if any(labels[i] == p for i in c)) for p in people]))


def _mixed(clusters: list[list[int]], labels: np.ndarray) -> list[set[int]]:
    """Clusters holding more than one person (strangers aside)."""
    return [people for c in clusters if len(people := {int(labels[i]) for i in c if labels[i] >= 0}) > 1]


@pytest.mark.fast
def test_leaf_alone_splits_one_person_into_many_groups() -> None:
    """The problem: leaf selection hands back a group per sub-mode."""
    for seed in range(3):
        vecs, labels = _photo_library(seed)
        assert len(vecs) > LARGE_INPUT_THRESHOLD
        clusters = _cluster_face_embeddings(vecs, min_cluster_size=3)
        assert _groups_per_person(clusters, labels) > 2.5
        assert _mixed(clusters, labels) == []


@pytest.mark.fast
def test_merging_close_clusters_reunites_each_person() -> None:
    """At the default distance each person is one group, or very nearly,
    and no group holds two people."""
    for seed in range(3):
        vecs, labels = _photo_library(seed)
        clusters = _cluster_face_embeddings(vecs, min_cluster_size=3, merge_epsilon=DEFAULT_MERGE_DISTANCE)
        assert _groups_per_person(clusters, labels) <= 1.1
        assert _mixed(clusters, labels) == []
        # Faces left out of every cluster stay out: merging adds none.
        alone = _cluster_face_embeddings(vecs, min_cluster_size=3)
        assert sum(map(len, clusters)) == sum(map(len, alone))


@pytest.mark.fast
def test_merging_keeps_close_relatives_apart() -> None:
    """Siblings whose centers are closer than this library's (cos 0.7) stay two people."""
    for seed in range(3):
        vecs, labels = _photo_library(seed, sibling_cos=0.7)
        clusters = _cluster_face_embeddings(vecs, min_cluster_size=3, merge_epsilon=DEFAULT_MERGE_DISTANCE)
        assert _mixed(clusters, labels) == []


@pytest.mark.fast
def test_too_wide_a_distance_merges_people() -> None:
    """Why the distance has an upper bound: far enough, people merge."""
    vecs, labels = _photo_library(0)
    clusters = _cluster_face_embeddings(vecs, min_cluster_size=3, merge_epsilon=0.65)
    assert _mixed(clusters, labels)


@pytest.mark.fast
def test_no_epsilon_is_leaf_selection_as_before() -> None:
    vecs, _ = _photo_library(1)
    assert _cluster_face_embeddings(vecs, min_cluster_size=3, merge_epsilon=0.0) == \
        _cluster_face_embeddings(vecs, min_cluster_size=3)


@pytest.mark.fast
def test_merging_does_not_chain_through_a_bridge() -> None:
    """The union-find regression, with merging on: one face between two
    people can't join them (it has no dense neighbourhood of its own)."""
    rng = np.random.default_rng(11)
    a_center = _mix(rng, np.eye(512)[0], 1.0)
    b_center = _mix(rng, a_center, 0.3)
    a = np.array([_mix(rng, a_center, 0.7) for _ in range(40)])
    b = np.array([_mix(rng, b_center, 0.7) for _ in range(40)])
    bridge = (a_center + b_center) / np.linalg.norm(a_center + b_center)
    vecs = np.vstack([a, b, bridge[None, :]]).astype(np.float32)
    clusters = _cluster_face_embeddings(vecs, min_cluster_size=3, merge_epsilon=DEFAULT_MERGE_DISTANCE)
    for c in clusters:
        assert not ({i for i in c if i < 40} and {i for i in c if 40 <= i < 80}), "identities merged via bridge"


@pytest.mark.fast
def test_small_inputs_are_not_merged() -> None:
    """Up to LARGE_INPUT_THRESHOLD faces the EOM path runs, as before."""
    rng = np.random.default_rng(12)
    center = np.zeros(32); center[0] = 1.0
    vecs = _blob(rng, center, 20, jitter=0.03)
    assert _cluster_face_embeddings(vecs, min_cluster_size=3, merge_epsilon=0.5) == \
        _cluster_face_embeddings(vecs, min_cluster_size=3)
