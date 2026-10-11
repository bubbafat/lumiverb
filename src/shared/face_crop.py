"""A face's crop: the 128 × 128 WebP the People pages show, cut from the
image the face was found in. The server cuts a photo's from its proxy
(routers/assets.py); the video faces producer cuts a video's from the frame
it was seen in (src/producers/video_faces), since the server has no frames."""

from __future__ import annotations

import io
from typing import Any

SIZE = 128
# Room around the face's box, as a share of its longer side.
PAD = 0.4


def face_crop(image: Any, box: dict[str, float]) -> bytes | None:
    """The crop of the face in box ({x, y, w, h}, fractions of the image) from
    image (a PIL RGB image): square around the box with PAD to spare, clamped
    to the image, as WebP. None when the box leaves nothing to cut."""
    from PIL import Image

    w, h = image.size
    cx, cy = box["x"] + box["w"] / 2, box["y"] + box["h"] / 2
    side = max(box["w"], box["h"]) * (1 + PAD)
    x1, y1 = max(0.0, cx - side / 2), max(0.0, cy - side / 2)
    x2, y2 = min(1.0, cx + side / 2), min(1.0, cy + side / 2)
    px1, py1, px2, py2 = int(x1 * w), int(y1 * h), int(x2 * w), int(y2 * h)
    if px2 <= px1 or py2 <= py1:
        return None
    crop = image.crop((px1, py1, px2, py2)).resize((SIZE, SIZE), Image.LANCZOS)
    buf = io.BytesIO()
    crop.save(buf, format="WEBP", quality=80)
    return buf.getvalue()
