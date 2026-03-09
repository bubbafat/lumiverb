"""
Model registry: maps vision_model_id to caption and embedding providers.

This is the single place where model capabilities are declared.
Workers, the similarity endpoint, and the enqueue logic all resolve
behavior through this registry — no scattered if/elif chains.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelConfig:
    # Human-readable display name
    display_name: str
    # Caption provider key (used by vision worker to select implementation)
    caption_provider: str
    # Embedding provider key (used by embedding worker)
    embedding_provider: str
    # Dimension of embedding vectors produced by this model's embedding provider
    embedding_dim: int
    # Default weights for hybrid similarity (must sum to 1.0)
    # moondream_weight + clip_weight = 1.0
    moondream_weight: float
    clip_weight: float
    # Model version string (stored in asset_metadata.model_version)
    model_version: str


REGISTRY: dict[str, ModelConfig] = {
    "moondream": ModelConfig(
        display_name="Moondream 2",
        caption_provider="moondream",
        embedding_provider="moondream",
        embedding_dim=512,
        moondream_weight=0.3,
        clip_weight=0.7,
        model_version="2",
    ),
    "qwen": ModelConfig(
        display_name="Qwen VL (LM Studio)",
        caption_provider="qwen_lmstudio",
        # Qwen doesn't produce embeddings; use CLIP for the embedding side
        embedding_provider="clip",
        embedding_dim=512,
        moondream_weight=0.0,
        clip_weight=1.0,
        model_version="1",
    ),
}


def get_model_config(vision_model_id: str) -> ModelConfig:
    """
    Return ModelConfig for a vision_model_id.
    Raises KeyError with a helpful message if unknown.
    """
    try:
        return REGISTRY[vision_model_id]
    except KeyError:
        known = ", ".join(REGISTRY.keys())
        raise KeyError(
            f"Unknown vision_model_id={vision_model_id!r}. Known models: {known}"
        )


# Convenience: all valid model IDs for validation
VALID_MODEL_IDS: frozenset[str] = frozenset(REGISTRY.keys())

