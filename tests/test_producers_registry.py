"""The producer registry: one producer per artifact kind, and a settings hash
that changes exactly when output-affecting settings do (ADR-016 phase 3)."""

from __future__ import annotations

import pytest

from src.shared import producers as P

pytestmark = pytest.mark.fast


def test_one_producer_per_artifact_and_each_artifact_once():
    assert set(P.ARTIFACTS) == {
        "lumiverb.video.format", "lumiverb.capture_time", "lumiverb.preview", "lumiverb.video.preview", "lumiverb.video.analysis_proxy", "lumiverb.video.scenes", "lumiverb.video.scene_descriptions",
        "lumiverb.image.description", "lumiverb.image.text", "lumiverb.image.likeness", "lumiverb.image.faces", "lumiverb.video.faces", "lumiverb.video.transcript", "lumiverb.location.guess",
        "lumiverb.video.description", "lumiverb.live_photo", "lumiverb.video.scene_likeness",
    }
    # A producer is named by what it makes (ADR-024): its artifact is its name.
    assert all(a == p.makes.name for a, p in P.PRODUCERS.items())


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
        "lumiverb.image.description": "vision", "lumiverb.image.text": "vision", "lumiverb.video.scene_descriptions": "vision",
        "lumiverb.video.description": "vision", "lumiverb.video.transcript": "transcripts", "lumiverb.image.likeness": "clip",
        "lumiverb.video.scene_likeness": "clip", "lumiverb.image.faces": "faces", "lumiverb.video.faces": "faces"}  # the AI jobs keep their words
    assert all(p.job in JOBS for p in P.PRODUCERS.values() if p.job)
    models = {"vision": "qwen3-vl:8b", "transcripts": "medium"}
    s = P.effective_settings("lumiverb.image.description", account=models)
    assert s["model"] == "qwen3-vl:8b" and s["prompt"] == P.VISION_PROMPT
    assert P.effective_settings("lumiverb.video.transcript", account=models)["model"] == "medium"
    # A job's model comes from the job alone: an override can't say another.
    assert P.effective_settings("lumiverb.image.description", overrides={"model": "llava:13b"}, account=models)["model"] == "qwen3-vl:8b"
    assert P.effective_settings("lumiverb.video.transcript", overrides={"model": "large-v3", "vad_min_silence_ms": 700},
                                account=models) == {"model": "medium", "vad_min_silence_ms": 700}
    # Only declared settings count: anything else can't change output.
    assert P.effective_settings("lumiverb.video.transcript", overrides={"nonsense": 1}) == P.PRODUCERS["lumiverb.video.transcript"].defaults


def test_a_job_without_a_model_leaves_the_producers_default():
    assert P.effective_settings("lumiverb.video.transcript", account={"vision": "qwen3-vl:8b"})["model"] == "small"
    assert P.effective_settings("lumiverb.video.transcript", account={"transcripts": ""})["model"] == "small"
    assert P.effective_settings("lumiverb.image.description", account={"transcripts": "medium"})["model"] == ""


def test_transcripts_made_with_small_stay_current():
    """The transcripts job starts at small, what every transcript so far recorded."""
    assert P.settings_hash(P.effective_settings("lumiverb.video.transcript", account={"transcripts": "small"})) == \
        P.settings_hash({"model": "small", "vad_min_silence_ms": 500})


def test_lineage_names_the_producer_version_settings_and_source():
    settings = P.effective_settings("lumiverb.video.transcript")
    assert P.lineage("lumiverb.video.transcript", settings, "sha256:" + "ab" * 32) == {
        # The producer by the name of what it makes (ADR-024 phase 1 part 2: servers take it from part 1).
        "producer": "lumiverb.video.transcript", "version": "1", "settings_hash": P.settings_hash(settings),
        "source_hash": "sha256:" + "ab" * 32,
    }


def test_clip_and_faces_are_all_or_nothing():
    # Scenes' vectors are compared with photos': one model across the library.
    assert {a for a, p in P.PRODUCERS.items() if p.uniform} == {"lumiverb.image.likeness", "lumiverb.video.scene_likeness",
                                                                "lumiverb.image.faces"}


def test_the_prompts_are_the_ones_the_worker_sends():
    from src.processing.workers.captions import openai_caption

    source = open(openai_caption.__file__).read()
    assert "Describe this image in 2-3 sentences" in P.VISION_PROMPT
    assert "What text is visible in this image?" in P.OCR_PROMPT
    # The worker takes its prompts from the registry, not copies of its own.
    assert "Describe this image in 2-3 sentences" not in source


def test_only_probe_capture_live_photos_and_the_analysis_proxy_read_the_originals():
    """Transcripts, scenes and their descriptions read the analysis proxy in
    this machine's cache: they run while the storage is away."""
    assert {a for a, p in P.PRODUCERS.items() if p.storage} == {
        "lumiverb.video.format", "lumiverb.capture_time", "lumiverb.live_photo", "lumiverb.video.analysis_proxy"}
    assert not any(P.PRODUCERS[a].storage for a in ("lumiverb.video.transcript", "lumiverb.video.scenes", "lumiverb.video.scene_descriptions"))
