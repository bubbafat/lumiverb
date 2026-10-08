"""The producer registry: one producer per artifact kind, and a settings hash
that changes exactly when output-affecting settings do (ADR-016 phase 3)."""

from __future__ import annotations

import pytest

from src.shared import producers as P

pytestmark = pytest.mark.fast


def test_one_producer_per_artifact_and_each_artifact_once():
    assert set(P.ARTIFACTS) == {
        "probe", "proxy", "video_preview", "analysis_proxy", "scenes", "scene_vision",
        "vision", "ocr", "clip", "faces", "transcript",
    }
    assert len({p.producer for p in P.PRODUCERS.values()}) == len(P.PRODUCERS)


def test_the_hash_ignores_key_order_and_follows_any_value():
    a = {"model": "small", "vad_min_silence_ms": 500}
    assert P.settings_hash(a) == P.settings_hash(dict(reversed(list(a.items()))))
    assert P.settings_hash(a) != P.settings_hash({**a, "model": "medium"})
    assert P.settings_hash(a) != P.settings_hash({**a, "vad_min_silence_ms": 501})
    assert len(P.settings_hash(a)) == 16


def test_the_account_fills_the_vision_model_and_overrides_win():
    s = P.effective_settings("vision", account={"model": "qwen3-vl:8b"})
    assert s["model"] == "qwen3-vl:8b" and s["prompt"] == P.VISION_PROMPT
    s = P.effective_settings("vision", overrides={"model": "llava:13b"}, account={"model": "qwen3-vl:8b"})
    assert s["model"] == "llava:13b"
    # Only declared settings count: anything else can't change output.
    assert P.effective_settings("transcript", overrides={"nonsense": 1}) == P.PRODUCERS["transcript"].defaults


def test_the_account_only_fills_what_comes_from_it():
    assert P.effective_settings("transcript", account={"model": "large"})["model"] == "small"


def test_lineage_names_the_producer_version_settings_and_source():
    settings = P.effective_settings("transcript")
    assert P.lineage("transcript", settings, "ab" * 32) == {
        "producer": "whisper", "version": "1", "settings_hash": P.settings_hash(settings),
        "source_sha256": "ab" * 32,
    }


def test_clip_and_faces_are_all_or_nothing():
    assert {a for a, p in P.PRODUCERS.items() if p.uniform} == {"clip", "faces"}


def test_the_prompts_are_the_ones_the_worker_sends():
    from src.client.workers.captions import openai_caption

    source = open(openai_caption.__file__).read()
    assert "Describe this image in 2-3 sentences" in P.VISION_PROMPT
    assert "What text is visible in this image?" in P.OCR_PROMPT
    # The worker takes its prompts from the registry, not copies of its own.
    assert "Describe this image in 2-3 sentences" not in source
