"""What Settings › AI's Test sends a machine (src/server/api/routers/ai.py
try_machine): a picture Lumiverb draws itself, nobody's photo, with words on
it to read, and a video's scenes as its producer would send them. The same
every time, so one machine's answers can be set beside another's."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

# Words on the picture's sign: what reading its text should find.
SIGN = "OPEN 9 TO 5"

# A short video's scenes, in order, as lumiverb.video.description sends them (video_description/work.py
# scenes_text).
SCENES = (
    "1. (0:00) A red house with a white door stands on a green hill under a blue sky. [tags: house, hill, sky]\n"
    "2. (0:06) A sign beside the door reads OPEN 9 TO 5. [tags: sign, door]\n"
    "3. (0:11) The sun sets behind the hill and the sky turns orange. [tags: sunset, hill]"
)


def picture() -> bytes:
    """A 1280 × 800 JPEG: a red house with a white door on a green hill, the sun, and a sign reading SIGN."""
    img = Image.new("RGB", (1280, 800), (120, 180, 235))
    draw = ImageDraw.Draw(img)
    draw.ellipse((1020, 70, 1160, 210), fill=(255, 215, 60))  # the sun
    draw.ellipse((-300, 520, 1580, 1300), fill=(70, 150, 60))  # the hill
    draw.rectangle((420, 330, 760, 600), fill=(190, 40, 40))  # the house
    draw.polygon([(390, 340), (590, 180), (790, 340)], fill=(110, 60, 40))  # its roof
    draw.rectangle((555, 470, 625, 600), fill=(250, 250, 250))  # its door
    draw.rectangle((820, 430, 1150, 540), fill=(255, 255, 255), outline=(30, 30, 30), width=6)  # the sign
    draw.line((985, 540, 985, 640), fill=(90, 60, 30), width=12)  # its post
    draw.text((985, 485), SIGN, fill=(20, 20, 20), anchor="mm", font=ImageFont.load_default(size=44))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue()
