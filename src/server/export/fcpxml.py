"""FCPXML 1.8 for Final Cut Pro: an event of asset clips, plus a project.

Version 1.8 (media path in asset@src, before 1.9's <media-rep>) is what
both Final Cut and Resolve import across releases.

The project lays every clip end to end in bin order on the first clip's
frame grid: Resolve imports FCPXML as timelines too. It holds the clips;
it isn't an edit (ADR-016).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from src.server.export.base import ExportBin, ExportClip, timeline_frames, timeline_lead


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
        root = ET.Element("fcpxml", version="1.8")
        resources = ET.SubElement(root, "resources")
        event = ET.SubElement(ET.SubElement(root, "library"), "event", name=bin_.name)

        formats: dict[tuple, str] = {}
        assets: list[tuple[ExportClip, str, str]] = []  # (clip, asset id, format id)
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
            ET.SubElement(
                resources,
                "asset",
                {
                    "id": asset_id,
                    "name": clip.name,
                    "start": start,
                    "duration": duration,
                    "hasVideo": "1",
                    "src": clip.file_url,
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
            ET.SubElement(
                event,
                "asset-clip",
                ref=asset_id,
                name=clip.name,
                start=start,
                duration=duration,
                format=formats[key],
                tcFormat="DF" if clip.is_drop_frame else "NDF",
            )
            assets.append((clip, asset_id, formats[key]))

        if assets:
            lead = timeline_lead([clip for clip, _, _ in assets])
            lead_format = next(f for clip, _, f in assets if clip is lead)
            num, den = lead.rate
            project = ET.SubElement(event, "project", name=bin_.name)
            sequence = ET.SubElement(
                project, "sequence", format=lead_format, tcStart="0s", tcFormat="NDF"
            )
            spine = ET.SubElement(sequence, "spine")
            position = 0
            for clip, asset_id, format_id in assets:
                length = timeline_frames(clip, lead)
                ET.SubElement(
                    spine,
                    "asset-clip",
                    ref=asset_id,
                    name=clip.name,
                    offset=f"{position * den}/{num}s",
                    start=_seconds(clip.start_frames, clip),
                    duration=f"{length * den}/{num}s",
                    format=format_id,
                    tcFormat="DF" if clip.is_drop_frame else "NDF",
                )
                position += length
            sequence.set("duration", f"{position * den}/{num}s")

        ET.indent(root)
        body = ET.tostring(root, encoding="unicode")
        return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + body + "\n").encode()
