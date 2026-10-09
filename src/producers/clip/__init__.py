"""Visual search: CLIP vectors of photos, on the brain's GPU."""

from src.producers.contract import IMAGE, ProducerSpec

# The model_id its vectors are stored under (asset_embeddings); other
# models' vectors (the macOS app's FeaturePrint) aren't its artifact.
CLIP_MODEL_ID = "clip"

PRODUCER = ProducerSpec(
    artifact="clip", producer="clip", version="1", media=IMAGE, title="Visual search (CLIP)", order=90,
    applies="a.media_type = 'image'",
    made=("EXISTS (SELECT 1 FROM asset_embeddings ae WHERE ae.asset_id = a.asset_id"
          f" AND ae.model_id = '{CLIP_MODEL_ID}')"),
    defaults={"model": "ViT-B-32", "pretrained": "openai", "input_edge": 1280}, uniform=True, needs=("proxy",),
    # CLIP and face detection take turns on the brain's GPU.
    kind="clip", flag="missing_embeddings", run="src.server.scheduler.runners:clip", pool="gpu",
)
