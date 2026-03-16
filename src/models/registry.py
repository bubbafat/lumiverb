"""
Model registry: embedding configuration.

All vision models use the OpenAI-compatible API; embeddings are always CLIP-only.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EmbeddingConfig:
    embedding_provider: str
    embedding_dim: int


CLIP_EMBEDDING_CONFIG = EmbeddingConfig(
    embedding_provider="clip",
    embedding_dim=512,
)


def get_embedding_config(vision_model_id: str) -> EmbeddingConfig:
    """Return EmbeddingConfig. All models use CLIP for embeddings."""
    return CLIP_EMBEDDING_CONFIG
