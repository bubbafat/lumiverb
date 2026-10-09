"""Text in photos (OCR), by the vision machines (Settings → AI)."""

from src.producers.contract import IMAGE, ProducerSpec
from src.producers.prompts import OCR_PROMPT, ai_settings

PRODUCER = ProducerSpec(
    artifact="ocr", producer="ocr", version="1", media=IMAGE, title="Text in images (OCR)", order=80,
    applies="a.media_type = 'image'",
    made="EXISTS (SELECT 1 FROM asset_ocr o WHERE o.asset_id = a.asset_id)",
    settings=ai_settings(OCR_PROMPT),
    job="vision", needs=("proxy",),
    kind="ocr", flag="missing_ocr", run="src.server.scheduler.runners:ocr", pool="vision", per_account=True,
)
