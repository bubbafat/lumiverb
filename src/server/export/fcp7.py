"""FCP7 XML (xmeml v5): a bin of master clips for DaVinci Resolve and Premiere Pro."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from src.server.export.base import ExportBin, ExportClip


def _sub(parent: ET.Element, tag: str, text: object | None = None, **attrs: str) -> ET.Element:
    el = ET.SubElement(parent, tag, attrs)
    if text is not None:
        el.text = str(text)
    return el


def _rate(parent: ET.Element, clip: ExportClip) -> None:
    rate = _sub(parent, "rate")
    _sub(rate, "timebase", clip.timebase)
    _sub(rate, "ntsc", "TRUE" if clip.ntsc else "FALSE")


def _clip(children: ET.Element, clip: ExportClip, index: int) -> None:
    master_id = f"masterclip-{index}"
    el = _sub(children, "clip", id=master_id)
    _sub(el, "masterclipid", master_id)
    _sub(el, "ismasterclip", "TRUE")
    _sub(el, "name", clip.name)
    _sub(el, "duration", clip.duration_frames)
    _rate(el, clip)

    track = _sub(_sub(_sub(el, "media"), "video"), "track")
    item = _sub(track, "clipitem", id=f"clipitem-{index}")
    _sub(item, "masterclipid", master_id)
    _sub(item, "name", clip.name)
    _sub(item, "duration", clip.duration_frames)
    _rate(item, clip)
    _sub(item, "in", 0)
    _sub(item, "out", clip.duration_frames)

    file_ = _sub(item, "file", id=f"file-{index}")
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
    video = _sub(media, "video")
    chars = _sub(video, "samplecharacteristics")
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

        # The master clip's audio, so it doesn't import as video only.
        atrack = _sub(_sub(el.find("media"), "audio"), "track")
        aitem = _sub(atrack, "clipitem", id=f"clipitem-{index}-audio")
        _sub(aitem, "masterclipid", master_id)
        _sub(aitem, "name", clip.name)
        _sub(aitem, "duration", clip.duration_frames)
        _rate(aitem, clip)
        _sub(aitem, "in", 0)
        _sub(aitem, "out", clip.duration_frames)
        _sub(aitem, "file", id=f"file-{index}")
        source = _sub(aitem, "sourcetrack")
        _sub(source, "mediatype", "audio")
        _sub(source, "trackindex", 1)


def _sequence(children: ET.Element, bin_: ExportBin) -> None:
    """Every clip end to end, in bin order: Resolve imports FCP7 XML as
    timelines, so a bin alone may bring in nothing. Not an edit; the
    editor owns order and trims (ADR-016)."""
    lead = bin_.clips[0]
    num, den = lead.rate
    fps = num / den
    seq = _sub(children, "sequence", id="sequence-1")
    _sub(seq, "name", bin_.name)
    _rate(seq, lead)
    video = _sub(_sub(seq, "media"), "video")
    vtrack = _sub(video, "track")
    atrack = _sub(_sub(seq.find("media"), "audio"), "track")
    position = 0
    for index, clip in enumerate(bin_.clips, start=1):
        length = round((clip.duration_sec or 0.0) * fps)
        for track, kind in ((vtrack, "video"), (atrack, "audio")):
            if kind == "audio" and not clip.audio_channels:
                continue
            item = _sub(track, "clipitem", id=f"seq-clipitem-{index}-{kind}")
            _sub(item, "masterclipid", f"masterclip-{index}")
            _sub(item, "name", clip.name)
            _rate(item, lead)
            _sub(item, "start", position)
            _sub(item, "end", position + length)
            _sub(item, "in", 0)
            _sub(item, "out", length)
            _sub(item, "file", id=f"file-{index}")
            if kind == "audio":
                source = _sub(item, "sourcetrack")
                _sub(source, "mediatype", "audio")
                _sub(source, "trackindex", 1)
        position += length
    _sub(seq, "duration", position)


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
        for index, clip in enumerate(bin_.clips, start=1):
            _clip(children, clip, index)
        if bin_.clips:
            _sequence(children, bin_)
        ET.indent(root)
        body = ET.tostring(root, encoding="unicode")
        return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + body + "\n").encode()
