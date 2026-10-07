"""FCPXML 1.10: an event of asset clips for Final Cut Pro."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from src.server.export.base import ExportBin, ExportClip


def _seconds(frames: int, clip: ExportClip) -> str:
    """Rational seconds for a frame count, in the rate's own units ("212212/30000s")."""
    num, den = clip.rate
    return f"{frames * den}/{num}s"


class FcpxmlProvider:
    id = "fcpxml"
    label = "Final Cut Pro"
    file_extension = ".fcpxml"
    content_type = "application/xml"

    def render(self, bin_: ExportBin) -> bytes:
        root = ET.Element("fcpxml", version="1.10")
        resources = ET.SubElement(root, "resources")
        event = ET.SubElement(ET.SubElement(root, "library"), "event", name=bin_.name)

        formats: dict[tuple, str] = {}
        next_id = 1
        for clip in bin_.clips:
            num, den = clip.rate
            key = (num, den, clip.width, clip.height)
            if key not in formats:
                format_id = f"r{next_id}"
                next_id += 1
                formats[key] = format_id
                attrs = {"id": format_id, "frameDuration": f"{den}/{num}s"}
                if clip.width and clip.height:
                    attrs.update(width=str(clip.width), height=str(clip.height))
                ET.SubElement(resources, "format", attrs)

            asset_id = f"r{next_id}"
            next_id += 1
            duration = _seconds(clip.duration_frames, clip)
            start = _seconds(clip.start_frames, clip)
            asset = ET.SubElement(
                resources,
                "asset",
                {
                    "id": asset_id,
                    "name": clip.name,
                    "start": start,
                    "duration": duration,
                    "hasVideo": "1",
                    "format": formats[key],
                    "hasAudio": "1" if clip.audio_channels else "0",
                    **(
                        {
                            "audioSources": "1",
                            "audioChannels": str(clip.audio_channels),
                            "audioRate": str(clip.audio_sample_rate or 48000),
                        }
                        if clip.audio_channels
                        else {}
                    ),
                },
            )
            ET.SubElement(asset, "media-rep", kind="original-media", src=clip.file_url)
            ET.SubElement(
                event,
                "asset-clip",
                ref=asset_id,
                name=clip.name,
                start=start,
                duration=duration,
                format=formats[key],
                tcFormat="DF" if clip.drop_frame else "NDF",
            )

        ET.indent(root)
        body = ET.tostring(root, encoding="unicode")
        return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + body + "\n").encode()
