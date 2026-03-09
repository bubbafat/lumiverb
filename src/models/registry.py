"""
Model registry: embedding configuration per vision model family.

Caption routing is handled by convention in the caption factory:
  "moondream"  → local Moondream SDK
  anything else → OpenAI-compatible vision API

The registry only needs to exist for embedding-side config.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EmbeddingConfig:
    embedding_provider: str   # "moondream" or "clip"
    embedding_dim: int
    moondream_weight: float
    clip_weight: float


# Embedding config keyed by vision model family prefix.
# For OpenAI-compatible models, "default" is used as fallback.
EMBEDDING_REGISTRY: dict[str, EmbeddingConfig] = {
    "moondream": EmbeddingConfig(
        embedding_provider="moondream",
        embedding_dim=512,
        moondream_weight=0.3,
        clip_weight=0.7,
    ),
    "default": EmbeddingConfig(
        embedding_provider="clip",
        embedding_dim=512,
        moondream_weight=0.0,
        clip_weight=1.0,
    ),
}


def get_embedding_config(vision_model_id: str) -> EmbeddingConfig:
    """
    Return EmbeddingConfig for a vision_model_id.
    Falls back to "default" for any unrecognised model ID.
    """
    return EMBEDDING_REGISTRY.get(vision_model_id, EMBEDDING_REGISTRY["default"])


def model_version_for_provenance(vision_model_id: str) -> str:
    """
    Return model_version for asset_metadata provenance.
    Moondream has real version numbers (2); OpenAI-compatible models use "1".
    """
    return "2" if vision_model_id == "moondream" else "1"
