"""Visual search: CLIP vectors of photos, by the CLIP job's machines (ADR-022):
the built-in machine (the scheduler's own model, on the server's GPU, as
always) and Lumiverb's AI workers (lumiverb-server ai-worker)."""

from src.producers import names
from src.producers.contract import IMAGE, NOT_READ_YET, Data, Field, ProducerSpec, Setting
from src.producers.pools import CLIP

WHOLE_LIBRARY = "Another model makes every photo's vectors again; not offered yet."

# The model_id its vectors are stored under (asset_embeddings); other
# models' vectors (the macOS app's FeaturePrint) aren't its artifact.
CLIP_MODEL_ID = "clip"

# The CLIP job's models (Settings › AI), by the name the job knows each by: open_clip's model and
# weights. What a machine doing the job may serve; the producer's settings are the one in use.
MODELS: dict[str, tuple[str, str]] = {"ViT-B-32": ("ViT-B-32", "openai")}

# The proxy cache's long edge: the images CLIP, faces and the vision
# machines are given (src/processing/proxy/proxy_cache.py).
PROXY_CACHE_EDGE = 1280

MAKES = Data(
    names.LIKENESS, "What visual search and Find similar compare: the photo's CLIP vector, and the model "
    "it's from.",
    (Field("vector", "vector"), Field("model", "text")),
    stored=("asset_embeddings",),
)

PRODUCER = ProducerSpec(
    makes=MAKES, reads=(names.PREVIEW,),
    artifact=names.LIKENESS, version="1", media=IMAGE, title="Visual search (CLIP)", order=90,
    applies="a.media_type = 'image'",
    made=("EXISTS (SELECT 1 FROM asset_embeddings ae WHERE ae.asset_id = a.asset_id"
          f" AND ae.model_id = '{CLIP_MODEL_ID}')"),
    settings=(
        Setting("model", "ViT-B-32", "Model", kind="text", fixed=WHOLE_LIBRARY),
        Setting("pretrained", "openai", "Weights", kind="text", fixed=WHOLE_LIBRARY),
        # The scheduler sends the proxy cache's size.
        Setting("input_edge", PROXY_CACHE_EDGE, "Image size", unit="px", advanced=True, fixed=NOT_READ_YET),
    ),
    uniform=True,
    # The CLIP job's machines (Settings › AI); on the server's GPU, the built-in one takes turns with faces.
    run="src.producers.clip.work:Clip", pool=CLIP,
)
