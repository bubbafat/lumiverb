"""Lumiverb's own data, named (ADR-024 phase 1, docs/adr/024-phase-1-plan.md).

Every built-in producer declares the name of what it makes (ProducerSpec.makes)
and the names it reads; what Lumiverb keeps without a producer (the original,
what the scan reads, a person's location, the location a clip shows, the
people in it) is named here. docs/data-names.md is written from both:
``uv run python -m src.producers --write``, never by hand.

Every artifact, producer, kind and file is stored and sent by its name (parts
3 and 4: the tenant migrations f2a6c8e4b0d7 and a3c9e5f1b7d2 renamed what was
stored, each with a mapping of its own). From part 5 nothing takes an old
spelling: WAS, what each name was called before, is the page's history and
what a refusal says to type instead (`now_called`), never a way in.

A person types a name, with or without `lumiverb.` (`typed`); an old word is
refused, naming the name.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from src.producers.contract import Data, Field

# The grammar of a name: lumiverb, then words (an output key's grammar), dots between.
NAME = re.compile(r"^lumiverb(\.[a-z][a-z0-9_]*)+$")
# A plug-in's id (src/plugins/manifest.py): its work is named by it.
PLUGIN_ID = re.compile(r"^[a-z][a-z0-9-]{0,39}$")

ORIGINAL = "lumiverb.original"
METADATA = "lumiverb.metadata"
CAPTURE_TIME = "lumiverb.capture_time"
PREVIEW = "lumiverb.preview"
LOCATION = "lumiverb.location"
LOCATION_PERSON = "lumiverb.location.person"
LOCATION_GPS = "lumiverb.location.gps"
LOCATION_GUESS = "lumiverb.location.guess"
VIDEO_FORMAT = "lumiverb.video.format"
VIDEO_PREVIEW = "lumiverb.video.preview"
ANALYSIS_PROXY = "lumiverb.video.analysis_proxy"
SCENES = "lumiverb.video.scenes"
SCENE_DESCRIPTIONS = "lumiverb.video.scene_descriptions"
TRANSCRIPT = "lumiverb.video.transcript"
VIDEO_FACES = "lumiverb.video.faces"
DESCRIPTION = "lumiverb.image.description"
TEXT = "lumiverb.image.text"
LIKENESS = "lumiverb.image.likeness"
FACES = "lumiverb.image.faces"
PEOPLE = "lumiverb.people"

_N = "number"
POINT = (Field("lat", _N), Field("lon", _N))
BOX = Field("box", "record", (Field("x", _N), Field("y", _N), Field("w", _N), Field("h", _N)))

# What Lumiverb keeps that no producer makes.
LUMIVERB: tuple[Data, ...] = (
    Data(ORIGINAL, "The file itself, in its library. Never handed to a plug-in: the API serves no originals.",
         (Field("file", "file"),), made_by="the library"),
    Data(METADATA, "Its media type, size, length, camera and lens.",
         (Field("media_type", "text"), Field("width", _N), Field("height", _N), Field("duration_sec", _N),
          Field("camera_make", "text"), Field("camera_model", "text"), Field("lens", "text")),
         stored=("assets.width", "assets.height", "assets.duration_sec", "assets.camera_make",
                 "assets.camera_model", "assets.lens_model"),
         made_by="the scan (a video's length, its probe)"),
    Data(LOCATION, "Where it was: a person's location, else the file's GPS, else an applied guess (never a "
         "suggestion), with how sure, in meters.",
         (*POINT, Field("sure_m", _N), Field("source", "text")),
         made_by="Lumiverb", reads=(LOCATION_PERSON, LOCATION_GPS, LOCATION_GUESS)),
    Data(LOCATION_PERSON, "A location a person set: a point, a place, or the same as another clip's.",
         (*POINT, Field("place", "record", (Field("id", "text"), Field("label", "text"), Field("credit", "text")))),
         stored=("asset_location",), person=("lat", "lon", "place"), made_by="a person"),
    Data(LOCATION_GPS, "The file's own GPS, with its accuracy when the file says.",
         (*POINT, Field("accuracy_m", _N)),
         stored=("assets.gps_lat", "assets.gps_lon", "assets.gps_accuracy_m"),
         made_by="the scan (the capture producer reads the accuracy for older clips)"),
    Data(PEOPLE, "Who's in it, photo or video: the people its faces are named as.",
         (Field("person", "record", (Field("id", "text"), Field("name", "text"))), Field("confirmed", "yes or no")),
         stored=("people", "face_person_matches", "faces"), many=True, person=("person", "confirmed"),
         made_by="a person, or Lumiverb (a face matched to someone named)", reads=(FACES, VIDEO_FACES)),
)

# Each file a name has: the name itself for its main file, a part under it for another.
THUMBNAIL = "lumiverb.preview.thumbnail"
SCENE_PICTURE = "lumiverb.video.scenes.picture"
FILES: tuple[str, ...] = (PREVIEW, THUMBNAIL, VIDEO_PREVIEW, ANALYSIS_PROXY, SCENE_PICTURE)

# What each name was called until ADR-024's part 5, by family: its artifact's old id, its lineage producer's,
# its job's kind's, and its file's old kind. The page's history, and what a refusal names (now_called): never
# taken. Written out (it outlives the migrations' own mappings, which tests hold to it).
WAS: Mapping[str, Mapping[str, str]] = {
    VIDEO_FORMAT: {"artifact": "probe", "producer": "ffprobe", "kind": "probe"},
    CAPTURE_TIME: {"artifact": "capture", "producer": "exiftool", "kind": "capture"},
    PREVIEW: {"artifact": "proxy", "producer": "proxy", "file": "proxy"},
    THUMBNAIL: {"file": "thumbnail"},
    VIDEO_PREVIEW: {"artifact": "video_preview", "producer": "preview", "file": "video_preview"},
    ANALYSIS_PROXY: {"artifact": "analysis_proxy", "producer": "analysis-proxy", "kind": "render",
                     "file": "analysis_proxy"},
    SCENES: {"artifact": "scenes", "producer": "scene-detect", "kind": "scenes"},
    SCENE_PICTURE: {"file": "scene_rep"},
    SCENE_DESCRIPTIONS: {"artifact": "scene_vision", "producer": "scene-vision", "kind": "scene_vision"},
    DESCRIPTION: {"artifact": "vision", "producer": "vision", "kind": "vision"},
    TEXT: {"artifact": "ocr", "producer": "ocr", "kind": "ocr"},
    LIKENESS: {"artifact": "clip", "producer": "clip", "kind": "clip"},
    FACES: {"artifact": "faces", "producer": "insightface", "kind": "faces"},
    VIDEO_FACES: {"artifact": "video_faces", "producer": "insightface-video", "kind": "video_faces"},
    TRANSCRIPT: {"artifact": "transcript", "producer": "whisper", "kind": "transcript"},
    LOCATION_GUESS: {"artifact": "location", "producer": "location", "kind": "location"},
}
# A plug-in's work before part 3: `plugin_` and its id, each dash an underscore (an id has none of its own).
_ADDED_ID = re.compile(r"^plugin_([a-z][a-z0-9_]{0,39})$")


def now_called(word: str, family: str | None = None) -> str | None:
    """The name an old word (of this family: artifact, producer, kind, file; any when None) was renamed to, for a
    refusal to name: `vision` → lumiverb.image.description, `redo_vision` → redo:lumiverb.image.description,
    `plugin_<id>` → the plug-in's id (never an id a plug-in can't have: that one was left out, its work no
    producer). None for a word that was never one of these."""
    if word.startswith("redo_") and family in (None, "kind"):
        name = now_called(word.removeprefix("redo_"), "kind")
        return remakes_of(name) if name else None
    match = _ADDED_ID.match(word)
    if match and family in (None, "artifact", "producer", "kind"):
        plugin_id = match.group(1).replace("_", "-")
        return None if is_reserved(plugin_id) else plugin_id
    for name, was in WAS.items():
        if word in (was.values() if family is None else (was.get(family),)):
            return name
    return None


# What a plug-in's id may not be (from part 1, and after part 5, so written out): it shares a namespace with
# Lumiverb's. The machine's words; the pools; every old word (WAS); the first word of each name (a name may be
# typed without `lumiverb.`). tests/test_data_names.py holds it to them.
MACHINE_WORDS = ("all", "upkeep", "scans", "scan", "person", "unknown")
RESERVED: frozenset[str] = frozenset({
    *MACHINE_WORDS,
    "cpu", "exif", "transcripts",
    "analysis-proxy", "analysis_proxy", "capture", "clip", "exiftool", "faces", "ffprobe", "insightface",
    "insightface-video", "location", "ocr", "preview", "probe", "proxy", "render", "scene-detect", "scene-vision",
    "scene_rep", "scene_vision", "scenes", "thumbnail", "transcript", "video_faces", "video_preview", "vision",
    "whisper",
    "capture_time", "image", "metadata", "original", "people", "video",
})


# A producer's kind is its name, its remakes' redo:<name>.
REDO = "redo:"


def remakes_of(name: str) -> str:
    """The kind of a producer's remakes (redo:<name>)."""
    return REDO + name


def made_by_file(file: str) -> str:
    """The name whose file this is: the name itself for its main file, the name it's under for another (the
    thumbnail, the preview's: lumiverb.preview)."""
    return file if file in WAS and "artifact" in WAS[file] else file.rpartition(".")[0]


# The part of a name a person may leave out (`lumiverb pause image.description`).
PREFIX = "lumiverb."


def short(name: str) -> str:
    """A name as a person types it: without `lumiverb.`."""
    return name.removeprefix(PREFIX)


def typed(word: str, listed: Iterable[str]) -> tuple[str | None, str | None]:
    """What a person typed for a producer or switch: a name (with or without `lumiverb.`) or a plug-in's id, as
    listed (the server's own words). Returns it, or None when nothing listed is called that; and, for an old word
    (`vision`, refused since ADR-024's part 5), what to type instead."""
    listed = set(listed)
    for found in (word, PREFIX + word):
        if found in listed:
            return found, None
    meant = now_called(word, "artifact")  # a producer was typed by what it made (`preview` is a name's, now)
    return None, (f"{word} is called {short(meant)} now: type that" if meant else None)


def is_reserved(plugin_id: str) -> bool:
    """A plug-in id refused at install: one of RESERVED, or starting `lumiverb`."""
    return plugin_id.startswith("lumiverb") or plugin_id in RESERVED


def sql_spelling(name: str) -> str:
    """The one spelling of a name (or a plug-in's id) for where SQL needs an identifier or a bind parameter:
    each dot and dash an underscore. A spelling, never a second name: never stored, sent or typed. Anything
    else (a remake's kind, `redo:<name>`) is refused."""
    if not (NAME.match(name) or PLUGIN_ID.match(name)):
        raise ValueError(f"{name!r} is neither a name nor a plug-in's id")
    return name.replace(".", "_").replace("-", "_")


def makers() -> dict[str, Data]:
    """Every name made on this install by Lumiverb (its producers' and its own), each its Data."""
    from src.producers import registry

    made = {d.name: d for d in LUMIVERB}
    made.update({p.makes.name: p.makes for p in registry().values() if p.makes is not None})
    return made


def names() -> list[str]:
    """Every lumiverb.* name, in the page's order (alphabetical: a name beside the ones under it)."""
    return sorted(makers())


def needs_of(reads: Iterable[str], made_by: Mapping[str, str]) -> tuple[str, ...]:
    """The artifacts these names are made by (made_by: name to artifact), in reads' order: what the queue waits
    for. A name the scan, a person or Lumiverb makes adds none."""
    return tuple(dict.fromkeys(made_by[n] for n in reads if n in made_by))


# ---------------------------------------------------------------------------
# docs/data-names.md
# ---------------------------------------------------------------------------

PAGE = Path(__file__).resolve().parents[2] / "docs" / "data-names.md"
WRITE = "uv run python -m src.producers --write"
_MEDIA = {("image",): "photos", ("video",): "videos", ("image", "video"): "photos and videos"}


def _fields(fields: tuple[Field, ...]) -> str:
    def one(f: Field) -> str:
        inner = f" ({', '.join(one(i) for i in f.items)})" if f.items else ""
        return f"`{f.key}` {f.type}{inner}"

    return "; ".join(one(f) for f in fields)


def page() -> str:
    """docs/data-names.md as the code says it now."""
    from src.plugins.manifest import INPUT_NAMES
    from src.producers import registry

    own = {p.makes.name: p for p in registry().values() if p.makes is not None and not p.plugin}
    readers: dict[str, list[str]] = {}
    for p in own.values():
        for n in dict.fromkeys((*p.reads, *p.reads_others)):
            whose = ("its own clip's and other clips'" if n in p.reads and n in p.reads_others
                     else "other clips'" if n in p.reads_others else "")
            readers.setdefault(n, []).append(f"{p.title} ({whose})" if whose else p.title)
    for d in LUMIVERB:
        for n in d.reads:
            readers.setdefault(n, []).append(f"`{d.name}`")
    inputs = {name: word for word, name in INPUT_NAMES.items()}
    lines = [
        "# Lumiverb's data, by name",
        "",
        "Written from the producers (`src/producers/<artifact>/__init__.py`, each one's `makes` and `reads`) and",
        f"`src/producers/names.py` by `{WRITE}`. Don't edit it by hand: a test fails when it isn't what",
        "the code says (ADR-024 phase 1, `docs/adr/024-phase-1-plan.md`).",
        "",
        "Each `lumiverb.*` name is Lumiverb's own data, version 1, its fields a draft until a plug-in is first",
        "handed one (ADR-024 phase 3). A name that only one kind of clip has says which: `lumiverb.image.*`",
        "(photos: `image`, as a clip's `media_type` says) or `lumiverb.video.*`. What a reader would be handed is",
        "what the clip shows now: where a person wrote it, theirs.",
        "",
        "Until ADR-024 phase 1's part 5 each was also taken by what it was called before (\"Was\" below, its",
        "history): since then the API refuses an old spelling, saying the name.",
        "",
    ]
    for name in names():
        made = makers()[name]
        p = own.get(name)
        media = _MEDIA.get(tuple(p.media), "") if p else ""
        lines += [f"## `{name}`", "", made.about, ""]
        lines.append(f"- **{'A list, each with' if made.many else 'Fields'}:** {_fields(made.fields)}.")
        if made.person:
            lines.append(f"- **A person can write:** {', '.join(f'`{k}`' for k in made.person)}: then theirs is shown.")
        lines.append(f"- **Made by:** {p.title if p else made.made_by}{f' ({media})' if media else ''}.")
        if p and p.reads:
            lines.append(f"- **Made from:** {', '.join(f'`{n}`' for n in p.reads)}"
                         f"{'; of other clips, ' + ', '.join(f'`{n}`' for n in p.reads_others) if p.reads_others else ''}.")
        elif made.reads:
            lines.append(f"- **Made from:** {', '.join(f'`{n}`' for n in made.reads)}.")
        if name in readers:
            lines.append(f"- **Read by:** {', '.join(readers[name])}.")
        if name in inputs:
            lines.append(f"- **Protocol 1's input:** `{inputs[name]}` (a plug-in's manifest says it that way).")
        was = WAS.get(name, {})
        if "artifact" in was:
            also = [f"lineage's producer `{was['producer']}`" if was["producer"] != was["artifact"] else "",
                    f"its job's kind `{was['kind']}`" if was.get("kind", was["artifact"]) != was["artifact"] else ""]
            also = [a for a in also if a]
            lines.append(f"- **Was:** `{was['artifact']}`{' (' + ', '.join(also) + ')' if also else ''}.")
        files = [f for f in FILES if f == name or f.startswith(name + ".")]
        if files:
            lines.append("- **Files:** " + ", ".join(f"`{f}` (was `{WAS[f]['file']}`)" for f in files) + ".")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if argv == ["--write"]:
        PAGE.write_text(page())
        print(f"✓ {PAGE}")
        return 0
    print(page())
    return 0

