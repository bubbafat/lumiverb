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


def test_each_ai_producer_takes_its_jobs_model():
    """One model per job (Settings → AI): the job's, wherever else a model is said."""
    from src.shared.ai_jobs import JOBS

    assert {a: p.job for a, p in P.PRODUCERS.items() if p.job} == {
        "vision": "vision", "ocr": "vision", "scene_vision": "vision", "transcript": "transcripts"}
    assert all(p.job in JOBS for p in P.PRODUCERS.values() if p.job)
    models = {"vision": "qwen3-vl:8b", "transcripts": "medium"}
    s = P.effective_settings("vision", account=models)
    assert s["model"] == "qwen3-vl:8b" and s["prompt"] == P.VISION_PROMPT
    assert P.effective_settings("transcript", account=models)["model"] == "medium"
    # A job's model comes from the job alone: an override can't say another.
    assert P.effective_settings("vision", overrides={"model": "llava:13b"}, account=models)["model"] == "qwen3-vl:8b"
    assert P.effective_settings("transcript", overrides={"model": "large-v3", "vad_min_silence_ms": 700},
                                account=models) == {"model": "medium", "vad_min_silence_ms": 700}
    # Only declared settings count: anything else can't change output.
    assert P.effective_settings("transcript", overrides={"nonsense": 1}) == P.PRODUCERS["transcript"].defaults


def test_a_job_without_a_model_leaves_the_producers_default():
    assert P.effective_settings("transcript", account={"vision": "qwen3-vl:8b"})["model"] == "small"
    assert P.effective_settings("transcript", account={"transcripts": ""})["model"] == "small"
    assert P.effective_settings("vision", account={"transcripts": "medium"})["model"] == ""


def test_transcripts_made_with_small_stay_current():
    """The transcripts job starts at small, what every transcript so far recorded."""
    assert P.settings_hash(P.effective_settings("transcript", account={"transcripts": "small"})) == \
        P.settings_hash({"model": "small", "vad_min_silence_ms": 500})


def test_lineage_names_the_producer_version_settings_and_source():
    settings = P.effective_settings("transcript")
    assert P.lineage("transcript", settings, "ab" * 32) == {
        "producer": "whisper", "version": "1", "settings_hash": P.settings_hash(settings),
        "source_sha256": "ab" * 32,
    }


def test_clip_and_faces_are_all_or_nothing():
    assert {a for a, p in P.PRODUCERS.items() if p.uniform} == {"clip", "faces"}


def test_the_prompts_are_the_ones_the_worker_sends():
    from src.processing.workers.captions import openai_caption

    source = open(openai_caption.__file__).read()
    assert "Describe this image in 2-3 sentences" in P.VISION_PROMPT
    assert "What text is visible in this image?" in P.OCR_PROMPT
    # The worker takes its prompts from the registry, not copies of its own.
    assert "Describe this image in 2-3 sentences" not in source


def test_only_probe_and_the_analysis_proxy_read_the_originals():
    """Transcripts, scenes and their descriptions read the analysis proxy in
    this machine's cache: they run while the storage is away."""
    assert {a for a, p in P.PRODUCERS.items() if p.storage} == {"probe", "analysis_proxy"}
    assert not any(P.PRODUCERS[a].storage for a in ("transcript", "scenes", "scene_vision"))
