"""Descriptions and tags of photos, by the vision machines (Settings → AI)."""

from src.producers import names
from src.producers.contract import IMAGE, Data, Field, ProducerSpec
from src.producers.pools import VISION
from src.producers.prompts import VISION_PROMPT, ai_settings

MAKES = Data(
    names.DESCRIPTION, "A photo described, with tags.",
    (Field("description", "text"), Field("tags", "list", (Field("tag", "text"),))),
    stored=("asset_metadata",), person=("description", "tags"),
)

PRODUCER = ProducerSpec(
    makes=MAKES, reads=(names.PREVIEW,),
    artifact="vision", producer="vision", version="1", media=IMAGE, title="Descriptions and tags", order=70,
    applies="a.media_type = 'image'",
    made="EXISTS (SELECT 1 FROM asset_metadata am WHERE am.asset_id = a.asset_id)",
    settings=ai_settings(VISION_PROMPT),
    kind="vision", flag="missing_vision", run="src.producers.vision.work:Vision", pool=VISION,
)
