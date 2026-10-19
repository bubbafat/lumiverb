"""Text in photos (OCR), by the vision machines (Settings → AI)."""

from src.producers import names
from src.producers.contract import IMAGE, Data, Field, ProducerSpec
from src.producers.pools import VISION
from src.producers.prompts import OCR_PROMPT, ai_settings

MAKES = Data(
    names.TEXT, "The text read in a photo.",
    (Field("text", "text"),),
    stored=("asset_ocr",), person=("text",),
)

PRODUCER = ProducerSpec(
    makes=MAKES, reads=(names.PREVIEW,),
    artifact="ocr", producer="ocr", version="1", media=IMAGE, title="Text in images (OCR)", order=80,
    applies="a.media_type = 'image'",
    made="EXISTS (SELECT 1 FROM asset_ocr o WHERE o.asset_id = a.asset_id)",
    settings=ai_settings(OCR_PROMPT),
    kind="ocr", flag="missing_ocr", run="src.producers.ocr.work:Ocr", pool=VISION,
)
