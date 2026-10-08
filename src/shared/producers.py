"""Producers and the lineage of what they make (ADR-016 phase 3).

Every derived artifact is a function of four inputs: the original (its
SHA-256), the producer, the producer's version and the settings that change
its output. A producer is registered here once, for its one artifact kind,
with the media it applies to and its output-affecting settings. Whatever
writes an artifact records those four inputs beside it (its lineage); when
any of them differs from what's registered now, the artifact is stale.

Bump a producer's ``version`` when its code changes what it makes. Change a
default in ``defaults`` when a setting changes; the hash follows. Settings an
account changes are stored on the server and laid over these defaults.

Shared by the server (what's current) and the worker (what it makes and
records), so they agree on both.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# The prompts are output-affecting settings: changing one makes descriptions stale.
VISION_PROMPT = (
    "Describe this image in 2-3 sentences, being specific about "
    "the subject, setting, and mood. Then provide 5-10 descriptive "
    "tags. Respond only with valid JSON in this exact format:\n"
    '{"description": "...", "tags": ["tag1", "tag2", ...]}'
)
OCR_PROMPT = (
    "What text is visible in this image? "
    "Include text from signs, labels, products, screens, documents, or watermarks. "
    "If none, say NONE."
)

IMAGE = ("image",)
VIDEO = ("video",)
ALL = ("image", "video")


@dataclass(frozen=True)
class Producer:
    artifact: str  # the one artifact kind it makes
    producer: str  # its id, recorded in lineage
    version: str  # bump when the code changes its output
    media: tuple[str, ...]  # the media types it applies to
    title: str  # for people: "Transcripts"
    defaults: Mapping[str, Any] = field(default_factory=dict)  # output-affecting settings
    # Its output must come from one model across the library (an embedding
    # space): an upgrade can't be partial.
    uniform: bool = False
    # Settings whose empty default means "the account's": filled in by the server.
    from_account: tuple[str, ...] = ()


def _producers() -> tuple[Producer, ...]:
    vision = {"model": "", "prompt": VISION_PROMPT, "max_edge": 1280, "temperature": 0.2, "max_tokens": 500}
    return (
        Producer("probe", "ffprobe", "1", VIDEO, "Video facts (probe)"),
        Producer("proxy", "proxy", "1", ALL, "Proxies and thumbnails",
                 {"long_edge": 2048, "jpeg_quality": 75, "webp_quality": 80, "thumbnail_edge": 512}),
        Producer("video_preview", "preview", "1", VIDEO, "Video previews",
                 {"seconds": 10, "max_height": 720, "crf": 28, "audio_kbps": 128}),
        Producer("analysis_proxy", "analysis-proxy", "1", VIDEO, "Analysis proxies",
                 {"max_edge": 960, "fps_max": 30, "encoder": "libx264", "crf": 28, "audio_kbps_per_channel": 48}),
        Producer("scenes", "scene-detect", "1", VIDEO, "Scenes",
                 {"frame_width": 480, "fps": 1, "phash_threshold": 51, "phash_hash_size": 16,
                  "temporal_ceiling_sec": 30.0, "debounce_sec": 3.0}),
        Producer("scene_vision", "scene-vision", "1", VIDEO, "Scene descriptions", vision,
                 from_account=("model",)),
        Producer("vision", "vision", "1", IMAGE, "Descriptions and tags", vision, from_account=("model",)),
        Producer("ocr", "ocr", "1", IMAGE, "Text in images (OCR)",
                 {"model": "", "prompt": OCR_PROMPT, "max_edge": 1280, "temperature": 0.2, "max_tokens": 500},
                 from_account=("model",)),
        Producer("clip", "clip", "1", IMAGE, "Visual search (CLIP)",
                 {"model": "ViT-B-32", "pretrained": "openai", "input_edge": 1280}, uniform=True),
        Producer("faces", "insightface", "1", IMAGE, "Faces",
                 {"model": "buffalo_l", "det_size": 640, "max_detect_edge": 1280, "min_confidence": 0.5,
                  "min_area_fraction": 0.003, "min_face_pixels": 40, "min_relative_size": 0.15,
                  "min_sharpness": 15.0}, uniform=True),
        Producer("transcript", "whisper", "1", VIDEO, "Transcripts",
                 {"model": "small", "vad_min_silence_ms": 500}),
    )


PRODUCERS: dict[str, Producer] = {p.artifact: p for p in _producers()}
ARTIFACTS: tuple[str, ...] = tuple(PRODUCERS)

# Lineage a write carries when nothing says who made it (an old client, the
# macOS app today): never current, so the brain makes it again.
UNKNOWN = "unknown"
# A person, not a producer, made it (a transcript typed or pasted): current
# whatever the producer's settings, never regenerated over.
PERSON = "person"


def settings_hash(settings: Mapping[str, Any]) -> str:
    """A short, stable hash of output-affecting settings (key order doesn't matter)."""
    canonical = json.dumps(dict(settings), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def effective_settings(artifact: str, overrides: Mapping[str, Any] | None = None,
                       account: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The producer's defaults, then the account's values for its from_account
    settings (e.g. the vision model), then explicit overrides. Unknown keys
    in overrides are ignored: only declared settings change output."""
    p = PRODUCERS[artifact]
    out = dict(p.defaults)
    for key in p.from_account:
        if account and account.get(key):
            out[key] = account[key]
    for key, value in (overrides or {}).items():
        if key in p.defaults:
            out[key] = value
    return out


def lineage(artifact: str, settings: Mapping[str, Any], source_sha256: str | None) -> dict[str, Any]:
    """What a write records about how the artifact was made."""
    p = PRODUCERS[artifact]
    return {
        "producer": p.producer,
        "version": p.version,
        "settings_hash": settings_hash(settings),
        "source_sha256": source_sha256,
    }
