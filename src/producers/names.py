"""Lumiverb's own data, named (ADR-024 phase 1, docs/adr/024-phase-1-plan.md).

Every built-in producer declares the name of what it makes (ProducerSpec.makes)
and the names it reads; what Lumiverb keeps without a producer (the original,
what the scan reads, a person's location, the location a clip shows, the
people in it) is named here. docs/data-names.md is written from both:
``uv run python -m src.producers --write``, never by hand.

RENAMED is what each name was called before, by family (artifacts, lineage's
producers, the scheduler's kinds, files; an installed plug-in's work was
`plugin_<id>`). From part 3 every artifact, producer and kind is stored and
sent as its name (the tenant migration f2a6c8e4b0d7 renamed what was stored,
with a mapping of its own), and the old spellings are still taken on input,
mapped to the names (`artifact_in`, `producer_in`, `kind_in`), each family's
mapping only where that family is taken (one old word can be several things:
`clip` is an artifact, an AI job and a model's id). Files keep their kinds
until part 4 (`file_in`, `file_named`). Part 5 deletes RENAMED.

A person types a name, with or without `lumiverb.`, or an old word, which is
warned of (`typed`).
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

# Each file a name has: the name itself for its main file, a part under it for another. What it was called.
FILES: Mapping[str, str] = {
    PREVIEW: "proxy",
    "lumiverb.preview.thumbnail": "thumbnail",
    VIDEO_PREVIEW: "video_preview",
    ANALYSIS_PROXY: "analysis_proxy",
    "lumiverb.video.scenes.picture": "scene_rep",
}

# What each name was called, by family: old to new. Held to the registry by tests/test_data_names.py.
ARTIFACTS: Mapping[str, str] = {
    "probe": VIDEO_FORMAT, "capture": CAPTURE_TIME, "proxy": PREVIEW, "video_preview": VIDEO_PREVIEW,
    "analysis_proxy": ANALYSIS_PROXY, "scenes": SCENES, "scene_vision": SCENE_DESCRIPTIONS, "vision": DESCRIPTION,
    "ocr": TEXT, "clip": LIKENESS, "faces": FACES, "video_faces": VIDEO_FACES, "transcript": TRANSCRIPT,
    "location": LOCATION_GUESS,
}
# Lineage's producers, by the artifact each makes: (artifact, producer) to the name.
PRODUCERS: Mapping[tuple[str, str], str] = {
    ("probe", "ffprobe"): VIDEO_FORMAT, ("capture", "exiftool"): CAPTURE_TIME, ("proxy", "proxy"): PREVIEW,
    ("video_preview", "preview"): VIDEO_PREVIEW, ("analysis_proxy", "analysis-proxy"): ANALYSIS_PROXY,
    ("scenes", "scene-detect"): SCENES, ("scene_vision", "scene-vision"): SCENE_DESCRIPTIONS,
    ("vision", "vision"): DESCRIPTION, ("ocr", "ocr"): TEXT, ("clip", "clip"): LIKENESS,
    ("faces", "insightface"): FACES, ("video_faces", "insightface-video"): VIDEO_FACES,
    ("transcript", "whisper"): TRANSCRIPT, ("location", "location"): LOCATION_GUESS,
}
# The scheduler's kinds (a remake's kind, redo_<kind>, becomes redo:<name>).
KINDS: Mapping[str, str] = {
    "probe": VIDEO_FORMAT, "capture": CAPTURE_TIME, "render": ANALYSIS_PROXY, "scenes": SCENES,
    "scene_vision": SCENE_DESCRIPTIONS, "vision": DESCRIPTION, "ocr": TEXT, "clip": LIKENESS, "faces": FACES,
    "video_faces": VIDEO_FACES, "transcript": TRANSCRIPT, "location": LOCATION_GUESS,
}
RENAMED: Mapping[str, Mapping] = {"artifact": ARTIFACTS, "producer": PRODUCERS, "kind": KINDS,
                                  "file": {old: new for new, old in FILES.items()}}

# What a plug-in's id may not be (from part 1, and after part 5, so written out, not read from RENAMED): it shares
# a namespace with Lumiverb's. The machine's words; the pools; every old word RENAMED takes; the first word of each
# name (a name may be typed without `lumiverb.`). tests/test_data_names.py holds it to them.
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


# A plug-in's work before part 3: `plugin_` and its id, each dash an underscore (an id has none of its own).
_ADDED = "plugin_"
_ADDED_ID = re.compile(r"^plugin_([a-z][a-z0-9_]{0,39})$")


def _plugin_id(value: str) -> str | None:
    match = _ADDED_ID.match(value)
    return match.group(1).replace("_", "-") if match else None


def artifact_in(value: str) -> str:
    """An artifact as a client sent it or an older server answered it: its name. Its old id (`vision`), and an
    installed plug-in's work as `plugin_<id>`, are taken until part 5; anything else is as it is."""
    return ARTIFACTS.get(value) or _plugin_id(value) or value


def file_in(value: str) -> str:
    """A file's kind as a client sent it or a server answered it, by today's kind or its name: today's kind
    (until part 4)."""
    return FILES.get(value, value)


def producer_in(artifact: str, producer: str) -> str:
    """Lineage's producer for this artifact (by its name or its old id), as a client sent it: its name when it's
    the old id of the producer that makes it now (`whisper`, of lumiverb.video.transcript), or `plugin_<id>` of
    that plug-in's work. Any other (an older producer, a person, unknown) is left as it is."""
    artifact = artifact_in(artifact)
    if any(new == artifact and old == producer for (_, old), new in PRODUCERS.items()) \
            or _plugin_id(producer) == artifact:
        return artifact
    return producer


# What a kind was called: a producer's (`render`) or its remakes' (`redo_render`). A producer's kind is its name
# from part 3, its remakes' redo:<name>.
REDO = "redo:"


def remakes_of(name: str) -> str:
    """The kind of a producer's remakes (redo:<name>)."""
    return REDO + name


def kind_in(value: str) -> str:
    """A job's kind as a client sent it or an older server answered it: a name, or redo:<name>."""
    if value.startswith("redo_"):
        return remakes_of(kind_in(value.removeprefix("redo_")))
    return KINDS.get(value) or _plugin_id(value) or value


_FILE_NAMES: Mapping[str, str] = {old: new for new, old in FILES.items()}


def file_named(kind: str) -> str:
    """A file by its name, as a client asks for or sends it (`proxy` to `lumiverb.preview`)."""
    return _FILE_NAMES.get(kind, kind)


def made_by_file(kind: str) -> str:
    """The name whose file this is (its kind until part 4): the name itself for its main file, the name it's
    under for another (`thumbnail`, the preview's: lumiverb.preview)."""
    name = file_named(kind)
    return name if name in ARTIFACTS.values() else name.rpartition(".")[0]


# The part of a name a person may leave out (`lumiverb pause image.description`).
PREFIX = "lumiverb."


def short(name: str) -> str:
    """A name as a person types it: without `lumiverb.`."""
    return name.removeprefix(PREFIX)


def typed(word: str, listed: Iterable[str]) -> tuple[str | None, str | None]:
    """What a person typed for a producer or switch: a name (with or without `lumiverb.`), a plug-in's id, or
    an old word (`vision`, `plugin_<id>`). Returns how the server lists it (listed: the server's own words,
    whichever spelling it answers in), or None when nothing listed is called that; and a warning naming what
    to type instead when it was an old word (from part 5 an old word is refused)."""
    by_meaning = {artifact_in(x): x for x in listed}
    if word in by_meaning:
        return by_meaning[word], None
    if PREFIX + word in by_meaning:
        return by_meaning[PREFIX + word], None
    meant = ARTIFACTS.get(word) or _plugin_id(word)
    if meant in by_meaning:
        return by_meaning[meant], f"{word} is called {short(meant)} now: type that (the old word goes in a later release)"
    return None, None


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
    renamed = {new: old for old, new in ARTIFACTS.items()}
    was_producer = {new: old for (_, old), new in PRODUCERS.items()}
    was_kind = {new: old for old, new in KINDS.items()}
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
        "Until part 3 of phase 1 renames what's stored and sent, the API keeps each one's old id, and takes",
        "either (\"Was\" below); the old spellings end with part 5.",
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
        if name in renamed:
            also = [f"lineage's producer `{was_producer[name]}`" if was_producer.get(name, renamed[name]) != renamed[name]
                    else "", f"its job's kind `{was_kind[name]}`" if was_kind.get(name, renamed[name]) != renamed[name]
                    else ""]
            also = [a for a in also if a]
            lines.append(f"- **Was:** `{renamed[name]}`{' (' + ', '.join(also) + ')' if also else ''}.")
        files = [f for f in FILES if f == name or f.startswith(name + ".")]
        if files:
            lines.append("- **Files:** " + ", ".join(f"`{f}` (was `{FILES[f]}`)" for f in files) + ".")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if argv == ["--write"]:
        PAGE.write_text(page())
        print(f"✓ {PAGE}")
        return 0
    print(page())
    return 0

