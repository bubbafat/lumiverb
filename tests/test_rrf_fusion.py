"""Unit tests for the Reciprocal Rank Fusion helper used by hybrid
similarity search. Pure function — no DB, no AI, fast marker."""

from __future__ import annotations

import pytest

from src.server.api.routers.similarity import RRF_K, _rrf_fuse


@pytest.mark.fast
class TestRRFFusion:
    def test_empty_inputs(self) -> None:
        assert _rrf_fuse() == []
        assert _rrf_fuse([], []) == []

    def test_single_list_preserves_order(self) -> None:
        scene = [("a", 0.1), ("b", 0.2), ("c", 0.3)]
        fused = _rrf_fuse(scene)
        assert [aid for aid, _ in fused] == ["a", "b", "c"]

    def test_asset_in_both_lists_outranks_either_alone(self) -> None:
        # `b` is rank 1 in scene and rank 0 in face. `a` is only in
        # scene at rank 0. The RRF score for `b` should be the highest.
        scene = [("a", 0.1), ("b", 0.2), ("c", 0.3)]
        face = [("b", 0.05), ("d", 0.1)]
        fused = _rrf_fuse(scene, face)
        # b should win because it appears in both
        assert fused[0][0] == "b"

    def test_face_only_match_can_outrank_pure_scene(self) -> None:
        """The whole point: an identity match that scene search missed
        entirely should still surface in the top results."""
        scene = [("scene1", 0.1), ("scene2", 0.2), ("scene3", 0.3)]
        face = [("face1", 0.05)]  # not in scene at all
        fused = _rrf_fuse(scene, face)
        # face1 should appear in the top results despite being absent
        # from the scene list
        ids = [aid for aid, _ in fused]
        assert "face1" in ids
        # And ahead of scene3 which is bottom of the scene list
        assert ids.index("face1") < ids.index("scene3")

    def test_top_of_one_list_beats_bottom_of_other(self) -> None:
        """Rank-1 in either list should beat rank-100 in the other.
        That's the floor RRF guarantees."""
        scene = [("scene_top", 0.0)] + [(f"scene_{i}", float(i)) for i in range(1, 100)]
        face = [("face_top", 0.0)] + [(f"face_{i}", float(i)) for i in range(1, 100)]
        fused = _rrf_fuse(scene, face)
        # The two top-of-list candidates should be the top two in fused
        top_two = {fused[0][0], fused[1][0]}
        assert top_two == {"scene_top", "face_top"}

    def test_uses_rank_not_distance(self) -> None:
        """RRF must operate on rank, not raw distance — that's what
        keeps it stable across embedding models with different cosine
        distributions."""
        # Scene distances are tiny (0.01 ... 0.03), face distances are
        # large (0.5 ... 0.7) — but ranks are equal. Both lists should
        # contribute equally.
        scene = [("x", 0.01), ("y", 0.02), ("z", 0.03)]
        face = [("y", 0.5), ("z", 0.6), ("x", 0.7)]
        fused = _rrf_fuse(scene, face)
        # x: scene rank 0 + face rank 2  = 1/61 + 1/63
        # y: scene rank 1 + face rank 0  = 1/62 + 1/61
        # z: scene rank 2 + face rank 1  = 1/63 + 1/62
        # y should win
        assert fused[0][0] == "y"

    def test_scores_descending(self) -> None:
        scene = [("a", 0.1), ("b", 0.2)]
        face = [("c", 0.05), ("a", 0.1)]
        fused = _rrf_fuse(scene, face)
        scores = [s for _, s in fused]
        assert scores == sorted(scores, reverse=True)

    def test_rrf_constant_is_60(self) -> None:
        # Sanity check that we're using the standard k=60 from the
        # original Cormack et al. paper.
        assert RRF_K == 60
