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

from src.server.repository.tenant import _cluster_face_embeddings


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
