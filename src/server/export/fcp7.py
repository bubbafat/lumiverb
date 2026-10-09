"""FCP7 XML (xmeml v5) for DaVinci Resolve and Premiere Pro.

One bin holding a sequence (every clip end to end, in bin order) and the
master clips. Resolve imports FCP7 XML as timelines, so the sequence is
what brings the clips in; it holds them, it isn't an edit (ADR-016).
Importers resolve <file id> references in reading order, so the sequence
comes first and carries each file's full definition; the master clips
refer back to it by id. A photo is a video-only clip of STILL_SEC.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from src.server.export.base import ExportBin, ExportClip, timeline_frames, timeline_lead


def _sub(parent: ET.Element, tag: str, text: object | None = None, **attrs: str) -> ET.Element:
    el = ET.SubElement(parent, tag, attrs)
    if text is not None:
        el.text = str(text)
    return el


def _rate(parent: ET.Element, clip: ExportClip) -> None:
    rate = _sub(parent, "rate")
    _sub(rate, "timebase", clip.timebase)
    _sub(rate, "ntsc", "TRUE" if clip.ntsc else "FALSE")


def _file(parent: ET.Element, clip: ExportClip, index: int) -> None:
    """The full <file> definition: path, rate, length, timecode, media."""
    file_ = _sub(parent, "file", id=f"file-{index}")
    _sub(file_, "name", clip.name)
    # Premiere expects the file://localhost/ form; Resolve accepts it too.
    _sub(file_, "pathurl", clip.file_url.replace("file://", "file://localhost", 1))
    _rate(file_, clip)
    _sub(file_, "duration", clip.duration_frames)
    tc = _sub(file_, "timecode")
    _rate(tc, clip)
    _sub(tc, "string", clip.timecode or "00:00:00:00")
    _sub(tc, "frame", clip.start_frames)
    _sub(tc, "displayformat", "DF" if clip.is_drop_frame else "NDF")

    media = _sub(file_, "media")
    chars = _sub(_sub(media, "video"), "samplecharacteristics")
    _rate(chars, clip)
    if clip.width and clip.height:
        _sub(chars, "width", clip.width)
        _sub(chars, "height", clip.height)
    if clip.audio_channels:
        audio = _sub(media, "audio")
        achars = _sub(audio, "samplecharacteristics")
        _sub(achars, "depth", 16)
        _sub(achars, "samplerate", clip.audio_sample_rate or 48000)
        _sub(audio, "channelcount", clip.audio_channels)


def _file_ref(parent: ET.Element, index: int) -> None:
    _sub(parent, "file", id=f"file-{index}")


def _sourcetrack(item: ET.Element) -> None:
    source = _sub(item, "sourcetrack")
    _sub(source, "mediatype", "audio")
    _sub(source, "trackindex", 1)


def _sequence(children: ET.Element, bin_: ExportBin) -> None:
    lead = timeline_lead(bin_.clips)
    seq = _sub(children, "sequence", id="sequence-1")
    _sub(seq, "name", bin_.name)
    duration = _sub(seq, "duration")
    _rate(seq, lead)
    media = _sub(seq, "media")
    vtrack = _sub(_sub(media, "video"), "track")
    atrack = _sub(_sub(media, "audio"), "track")
    position = 0
    for index, clip in enumerate(bin_.clips, start=1):
        # Lengths on the sequence's own (lead clip's) frame grid.
        length = timeline_frames(clip, lead)
        for track, kind in ((vtrack, "video"), (atrack, "audio")):
            if kind == "audio" and not clip.audio_channels:
                continue
            item = _sub(track, "clipitem", id=f"seq-clipitem-{index}-{kind}")
            _sub(item, "masterclipid", f"masterclip-{index}")
            _sub(item, "name", clip.name)
            _sub(item, "duration", length)
            _rate(item, lead)
            _sub(item, "start", position)
            _sub(item, "end", position + length)
            _sub(item, "in", 0)
            _sub(item, "out", length)
            if kind == "video":
                _file(item, clip, index)
            else:
                _file_ref(item, index)
                _sourcetrack(item)
        position += length
    duration.text = str(position)


def _master_clip(children: ET.Element, clip: ExportClip, index: int) -> None:
    master_id = f"masterclip-{index}"
    el = _sub(children, "clip", id=master_id)
    _sub(el, "masterclipid", master_id)
    _sub(el, "ismasterclip", "TRUE")
    _sub(el, "name", clip.name)
    _sub(el, "duration", clip.duration_frames)
    _rate(el, clip)

    media = _sub(el, "media")
    kinds = ("video", "audio") if clip.audio_channels else ("video",)
    for kind in kinds:
        item = _sub(_sub(_sub(media, kind), "track"), "clipitem", id=f"clipitem-{index}-{kind}")
        _sub(item, "masterclipid", master_id)
        _sub(item, "name", clip.name)
        _sub(item, "duration", clip.duration_frames)
        _rate(item, clip)
        _sub(item, "in", 0)
        _sub(item, "out", clip.duration_frames)
        _file_ref(item, index)
        if kind == "audio":
            _sourcetrack(item)


class Fcp7XmlProvider:
    id = "fcp7"
    label = "DaVinci Resolve / Premiere Pro"
    file_extension = ".xml"
    content_type = "application/xml"

    def render(self, bin_: ExportBin) -> bytes:
        root = ET.Element("xmeml", version="5")
        bin_el = _sub(root, "bin")
        _sub(bin_el, "name", bin_.name)
        children = _sub(bin_el, "children")
        if bin_.clips:
            _sequence(children, bin_)
        for index, clip in enumerate(bin_.clips, start=1):
            _master_clip(children, clip, index)
        ET.indent(root)
        body = ET.tostring(root, encoding="unicode")
        return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + body + "\n").encode()
