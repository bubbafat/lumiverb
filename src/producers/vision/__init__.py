"""Descriptions and tags of photos, by the vision machines (Settings → AI)."""

from src.producers.contract import IMAGE, ProducerSpec
from src.producers.pools import VISION
from src.producers.prompts import VISION_PROMPT, ai_settings

PRODUCER = ProducerSpec(
    artifact="vision", producer="vision", version="1", media=IMAGE, title="Descriptions and tags", order=70,
    applies="a.media_type = 'image'",
    made="EXISTS (SELECT 1 FROM asset_metadata am WHERE am.asset_id = a.asset_id)",
    settings=ai_settings(VISION_PROMPT), needs=("proxy",),
    kind="vision", flag="missing_vision", run="src.producers.vision.work:Vision", pool=VISION,
)
