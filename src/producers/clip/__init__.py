"""Visual search: CLIP vectors of photos, on the brain's GPU."""

from src.producers.contract import IMAGE, NOT_READ_YET, ProducerSpec, Setting

WHOLE_LIBRARY = "Another model makes every photo's vectors again; not offered yet."

# The model_id its vectors are stored under (asset_embeddings); other
# models' vectors (the macOS app's FeaturePrint) aren't its artifact.
CLIP_MODEL_ID = "clip"

PRODUCER = ProducerSpec(
    artifact="clip", producer="clip", version="1", media=IMAGE, title="Visual search (CLIP)", order=90,
    applies="a.media_type = 'image'",
    made=("EXISTS (SELECT 1 FROM asset_embeddings ae WHERE ae.asset_id = a.asset_id"
          f" AND ae.model_id = '{CLIP_MODEL_ID}')"),
    settings=(
        Setting("model", "ViT-B-32", "Model", kind="text", fixed=WHOLE_LIBRARY),
        Setting("pretrained", "openai", "Weights", kind="text", fixed=WHOLE_LIBRARY),
        # The scheduler sends the proxy cache's size (PROXY_CACHE_EDGE).
        Setting("input_edge", 1280, "Image size", unit="px", advanced=True, fixed=NOT_READ_YET),
    ),
    uniform=True, needs=("proxy",),
    # CLIP and face detection take turns on the brain's GPU.
    kind="clip", flag="missing_embeddings", run="src.server.scheduler.runners:clip", pool="gpu",
)
