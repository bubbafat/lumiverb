"""A plug-in's manifest (ADR-023): lumiverb-plugin.json in its folder, read
and checked before any of its code runs, so what it reads and reaches is
shown and accepted first.

A plug-in is a program of its own, any language, run outside Lumiverb
(Robert, 2026-10-18: "They run outside for safety"). Its manifest says who
it is, how it's started, its acknowledgment (ADR-021's), what it downloads
and whether it uses the internet, what it may use (memory, disk), its
settings (each with a type, bounds, help text: Settings › Plug-ins builds
the form from them, and Lumiverb checks every value), what it does for each
clip (the inputs it's handed, the outputs it gives back) and the lookups it
answers. Everything here is protocol 1's: an input, an output's kind or a
lookup Lumiverb doesn't know is refused, as is any field it doesn't know.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from src.plugins import version_key
from src.plugins.contract import Acknowledgment, Link, PluginSpec

MANIFEST = "lumiverb-plugin.json"
PROTOCOLS = (1,)
# What a plug-in can be handed for each clip (protocol 1), in words for whoever installs or turns it on.
INPUTS = {
    "location": "each clip's location (a person's, else its GPS, else an applied guess)",
    "capture_time": "when each clip was taken",
    "metadata": "each clip's media type, size, duration, camera and lens",
    "preview": "each photo's preview image",
}
OUTPUT_KINDS = ("fact",)
LOOKUPS = {"places": "answers place searches (Set location's Place)"}
MEDIA = ("image", "video")
SETTING_TYPES = ("int", "float", "bool", "text", "choice", "secret")
SCOPES = ("account", "server")
MEMORY_MB, MEMORY_MB_MAX = 1024, 16384
DISK_MB, DISK_MB_MAX = 1000, 1_000_000
TEXT_MAX = 4000
_TOO_BIG = 256 * 1024  # bytes: a manifest is a page of JSON
_ID = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class ManifestError(ValueError):
    """What's wrong with a manifest: every problem found, each naming its field."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Choice:
    value: str
    label: str


@dataclass(frozen=True)
class PluginSetting:
    """A setting the plug-in declares: what Settings › Plug-ins shows, and
    what Lumiverb checks a value by before saving it or handing it on."""

    key: str
    type: str  # SETTING_TYPES
    default: Any
    label: str
    help: str
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""
    pattern: str = ""  # text: a regular expression the whole value matches
    max_length: int = TEXT_MAX  # text and secret
    choices: tuple[Choice, ...] = ()
    scope: str = "account"  # account: each account's admin; server: the server admin's
    advanced: bool = False  # folded away until asked for
    remakes: bool = False  # its outputs are made again when it changes

    def check(self, value: Any) -> Any:
        """The value as stored, or ValueError saying what's wrong with it."""
        if self.type == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{self.label} is yes or no (true or false)")
            return value
        if self.type == "choice":
            if value not in [c.value for c in self.choices]:
                raise ValueError(f"{self.label} is one of {', '.join(c.label for c in self.choices)}")
            return value
        if self.type in ("text", "secret"):
            if not isinstance(value, str):
                raise ValueError(f"{self.label} is text")
            if "\x00" in value or len(value) > self.max_length:
                raise ValueError(f"{self.label} is text, up to {self.max_length:,} characters")
            if self.type == "text" and not value.strip() and str(self.default).strip():
                raise ValueError(f"{self.label} can't be empty")
            if self.type == "text" and value and self.pattern and re.fullmatch(self.pattern, value) is None:
                raise ValueError(f"{self.label} isn't in the form it takes")
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{self.label} is a number")
        try:
            number = float(value)
        except OverflowError:  # a whole number too big for a float
            number = math.inf
        if not math.isfinite(number) or (self.minimum is not None and number < self.minimum) \
                or (self.maximum is not None and number > self.maximum):
            raise ValueError(self.bounds())
        if self.type == "int":
            if not number.is_integer():
                raise ValueError(f"{self.label} is a whole number")
            return int(number)
        return number

    def bounds(self) -> str:
        unit = f" {self.unit}" if self.unit else ""
        if self.minimum is None and self.maximum is None:
            return f"{self.label} is a finite number"
        if self.maximum is None:
            return f"{self.label} is at least {self.minimum:g}{unit}"
        if self.minimum is None:
            return f"{self.label} is at most {self.maximum:g}{unit}"
        return f"{self.label} is from {self.minimum:g} to {self.maximum:g}{unit}"


@dataclass(frozen=True)
class Download:
    url: str
    title: str
    about_mb: int


@dataclass(frozen=True)
class Credit:
    """Who to credit for what it shows or finds (a data source), and the license that asks it, if any: shown
    beside its facts and its places, and kept with a place a person picked."""

    text: str
    url: str = ""
    license: Link | None = None

    def shown(self) -> dict[str, Any]:
        """As the API says it: {text, url, license: {text, url} | None}."""
        return {"text": self.text, "url": self.url or None,
                "license": {"text": self.license.label, "url": self.license.url} if self.license else None}


@dataclass(frozen=True)
class Output:
    key: str
    kind: str  # OUTPUT_KINDS
    label: str
    searchable: bool = False
    # What's shown under its label when it has nothing to say for a clip ("Not near a town"): never searched.
    empty: str = ""


@dataclass(frozen=True)
class WorkSpec:
    """What it does for each clip: the inputs it's handed, the outputs it gives back."""

    title: str
    version: str  # goes up when its output would differ (a producer's version)
    media: tuple[str, ...]
    inputs: tuple[str, ...]
    batch: int
    outputs: tuple[Output, ...]


@dataclass(frozen=True)
class Manifest:
    plugin_id: str
    version: str
    protocol: int
    title: str
    description: str
    run: tuple[str, ...]  # its command, from its folder: a path in it, or a program's name
    author: str = ""
    homepage: str = ""
    acknowledgment: Acknowledgment | None = None
    credit: Credit | None = None
    downloads: tuple[Download, ...] = ()
    data_version: int = 1
    internet: bool = False
    memory_mb: int = MEMORY_MB
    disk_mb: int = DISK_MB
    check_settings: bool = False  # it checks new settings itself too (POST /v1/settings/check)
    settings: tuple[PluginSetting, ...] = ()
    work: WorkSpec | None = None
    lookups: tuple[str, ...] = ()

    def spec(self) -> PluginSpec:
        """As an account's switch sees it (repository/plugins.py): its id, title, text and acknowledgment."""
        return PluginSpec(plugin_id=self.plugin_id, title=self.title, description=self.description,
                          acknowledgment=self.acknowledgment, order=500)

    def setting(self, key: str) -> PluginSetting | None:
        return next((s for s in self.settings if s.key == key), None)

    def permissions(self) -> dict[str, Any]:
        """What it reads and reaches, as accepted at install: an update asking more asks again."""
        return {"inputs": sorted(self.work.inputs) if self.work else [],
                "download_hosts": sorted({urlsplit(d.url).hostname or "" for d in self.downloads}),
                "internet": self.internet, "lookups": sorted(self.lookups)}

    def described(self) -> list[str]:
        """What it reads, reaches and shows, in words, a line each (what the server admin accepts)."""
        lines = [f"Reads {INPUTS[i]}" for i in (self.work.inputs if self.work else ())]
        for d in self.downloads:
            lines.append(f"Downloads {d.title} (about {d.about_mb:,} MB) from {urlsplit(d.url).hostname}")
        lines.append("Uses the internet" if self.internet else "Doesn't use the internet")
        lines += [_first_up(LOOKUPS[name]) for name in self.lookups]
        if self.work:
            shown = ", ".join(f"{o.label}{' (searchable)' if o.searchable else ''}" for o in self.work.outputs)
            lines.append(f"Shows with each clip: {shown}")
        lines.append(f"Uses up to {self.memory_mb:,} MB of memory and {self.disk_mb:,} MB on disk")
        return lines


FACT_MAX = 200  # characters
EMPTY_MAX = 80  # an output's empty line
URL_MAX = 2000
_TAG = re.compile(r"<[A-Za-z/!?]")


def plain(words: str, most: int) -> bool:
    """Whether a fact's text is shown as it is: plain text (no control character, no HTML tag), 1 to most
    characters."""
    return 0 < len(words) <= most and not any(ord(c) < 32 or 127 <= ord(c) < 160 for c in words) \
        and not _TAG.search(words)


def checked_outputs(work: WorkSpec, outputs: Any) -> dict[str, dict[str, str] | None]:
    """What a plug-in gave back for one clip, as Lumiverb keeps it: each output it declares, a fact
    ({"text", "url"?}: plain text up to FACT_MAX characters, an http(s) link) or None (nothing to say; one
    it left out is that too). ValueError saying why for anything else: a key it didn't declare, a control
    character, HTML (a tag), a link that isn't http(s)."""
    if not isinstance(outputs, dict):
        raise ValueError("outputs: an object, by output key")
    declared = {o.key: o for o in work.outputs}
    unknown = sorted(k for k in outputs if k not in declared)
    if unknown:
        raise ValueError(f"outputs: {', '.join(map(str, unknown))[:200]} isn't one it declares")
    out: dict[str, dict[str, str] | None] = {}
    for key in declared:
        value = outputs.get(key)
        if value is None:
            out[key] = None
            continue
        if not isinstance(value, dict) or set(value) - {"text", "url"} or not isinstance(value.get("text"), str):
            raise ValueError(f"outputs.{key}: a fact, {{\"text\", \"url\"?}}, or null")
        words = " ".join(value["text"].split())
        if not plain(words, FACT_MAX):
            raise ValueError(f"outputs.{key}.text: plain text (no HTML), 1 to {FACT_MAX} characters")
        fact = {"text": words}
        if value.get("url") is not None:
            url = value["url"]
            if not isinstance(url, str) or len(url) > URL_MAX or urlsplit(url).scheme not in ("http", "https") \
                    or not urlsplit(url).netloc or any(c.isspace() or ord(c) < 32 for c in url):
                raise ValueError(f"outputs.{key}.url: an http or https link, up to {URL_MAX:,} characters")
            fact["url"] = url
        out[key] = fact
    return out


def asks_more(before: dict[str, Any], now: dict[str, Any]) -> list[str]:
    """What a newer version reads or reaches that the one accepted didn't, in words (empty: nothing more)."""
    more = [f"Reads {INPUTS.get(i, i)}" for i in now.get("inputs", []) if i not in before.get("inputs", [])]
    more += [f"Downloads from {h}" for h in now.get("download_hosts", []) if h not in before.get("download_hosts", [])]
    if now.get("internet") and not before.get("internet"):
        more.append("Uses the internet")
    more += [_first_up(LOOKUPS.get(n, n)) for n in now.get("lookups", []) if n not in before.get("lookups", [])]
    return more


def _first_up(words: str) -> str:
    return words[:1].upper() + words[1:]


class _Fields:
    """Reads one JSON object's fields, noting each problem with where it is."""

    def __init__(self, data: Any, where: str, problems: list[str], known: tuple[str, ...]) -> None:
        self.where, self.problems = where, problems
        if not isinstance(data, dict):
            problems.append(f"{where or 'the manifest'} is an object")
            data = {}
        self.data = data
        for key in data:
            if key not in known:
                problems.append(f"{self._at(key)}: not a field Lumiverb knows")

    def _at(self, key: str) -> str:
        return f"{self.where}.{key}" if self.where else key

    def bad(self, key: str, why: str) -> None:
        self.problems.append(f"{self._at(key)}: {why}")

    def has(self, key: str) -> bool:
        return key in self.data

    def text(self, key: str, *, required: bool = False, most: int = 200, default: str = "") -> str:
        value = self.data.get(key)
        if value is None:
            if required:
                self.bad(key, "required")
            return default
        if not isinstance(value, str) or "\x00" in value or len(value) > most:
            self.bad(key, f"text, up to {most:,} characters")
            return default
        if required and not value.strip():
            self.bad(key, "can't be empty")
        return value

    def boolean(self, key: str, default: bool = False) -> bool:
        value = self.data.get(key, default)
        if not isinstance(value, bool):
            self.bad(key, "true or false")
            return default
        return value

    def whole(self, key: str, default: int, least: int, most: int) -> int:
        value = self.data.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not least <= value <= most:
            self.bad(key, f"a whole number from {least:,} to {most:,}")
            return default
        return value

    def number(self, key: str) -> float | None:
        value = self.data.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            self.bad(key, "a finite number")
            return None
        return value

    def version(self, key: str, *, required: bool = True) -> str:
        value = self.text(key, required=required, most=40)
        if value and version_key(value) is None:
            self.bad(key, 'numbers with dots ("1", "2.1")')
        return value

    def url(self, key: str, *, required: bool = False) -> str:
        value = self.text(key, required=required, most=2000)
        if value and not _https(value):
            self.bad(key, "an https:// address")
        return value

    def items(self, key: str, *, most: int) -> list[tuple[str, Any]]:
        """A list's items, each with where it is."""
        value = self.data.get(key, [])
        if not isinstance(value, list) or len(value) > most:
            self.bad(key, f"a list of up to {most}")
            return []
        return [(f"{self._at(key)}[{n}]", item) for n, item in enumerate(value)]

    def names(self, key: str, known, *, most: int = 20) -> tuple[str, ...]:
        """A list of names, each one of known's."""
        out: list[str] = []
        for at, item in self.items(key, most=most):
            if not isinstance(item, str) or item not in known:
                self.problems.append(f"{at}: one of {', '.join(known)}")
            elif item in out:
                self.problems.append(f"{at}: {item} twice")
            else:
                out.append(item)
        return tuple(out)


def _https(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and bool(parts.hostname)


def _acknowledgment(data: Any, where: str, problems: list[str]) -> Acknowledgment | None:
    f = _Fields(data, where, problems, ("version", "title", "text", "links", "reaccept_from"))
    version = f.version("version")
    title = f.text("title", required=True)
    body = f.text("text", required=True, most=20000)
    since = f.version("reaccept_from", required=False)
    if version_key(version) and version_key(since) and version_key(since) > version_key(version):
        f.bad("reaccept_from", f"a version up to its own ({version})")
    links = []
    for at, item in f.items("links", most=10):
        link = _Fields(item, at, problems, ("label", "url"))
        links.append(Link(link.text("label", required=True, most=100), link.url("url", required=True)))
    return Acknowledgment(version=version, title=title, text=body, links=tuple(links), reaccept_from=since)


def _setting(data: Any, where: str, problems: list[str]) -> PluginSetting | None:
    f = _Fields(data, where, problems, ("key", "type", "default", "label", "help", "minimum", "maximum", "unit",
                                        "pattern", "max_length", "choices", "scope", "advanced", "remakes"))
    key = f.text("key", required=True, most=40)
    if key and not _KEY.match(key):
        f.bad("key", "lowercase letters, digits and underscores, starting with a letter")
    kind = f.text("type", required=True, most=20)
    if kind and kind not in SETTING_TYPES:
        f.bad("type", f"one of {', '.join(SETTING_TYPES)}")
        return None
    label, help_text = f.text("label", required=True, most=80), f.text("help", required=True, most=500)
    numeric = kind in ("int", "float")
    minimum, maximum = (f.number("minimum"), f.number("maximum")) if numeric else (None, None)
    for field in ("minimum", "maximum", "unit"):
        if f.has(field) and not numeric:
            f.bad(field, f"only for a number (int, float), not {kind}")
    if minimum is not None and maximum is not None and minimum > maximum:
        f.bad("minimum", "at most its maximum")
    pattern = f.text("pattern", most=500) if kind == "text" else ""
    if f.has("pattern") and kind != "text":
        f.bad("pattern", "only for text")
    if pattern:
        try:
            re.compile(pattern)
        except re.error as exc:
            f.bad("pattern", f"a regular expression ({exc})")
            pattern = ""
    max_length = f.whole("max_length", TEXT_MAX, 1, TEXT_MAX) if kind in ("text", "secret") else TEXT_MAX
    if f.has("max_length") and kind not in ("text", "secret"):
        f.bad("max_length", "only for text or a secret")
    choices: list[Choice] = []
    if kind == "choice":
        for at, item in f.items("choices", most=50):
            c = _Fields(item, at, problems, ("value", "label"))
            choice = Choice(c.text("value", required=True, most=100), c.text("label", required=True, most=80))
            if choice.value in [x.value for x in choices]:
                problems.append(f"{at}: {choice.value} twice")
            choices.append(choice)
        if not choices:
            f.bad("choices", "at least one, for a choice")
    elif f.has("choices"):
        f.bad("choices", "only for a choice")
    scope = f.text("scope", most=20, default="account")
    if scope not in SCOPES:
        f.bad("scope", f"one of {', '.join(SCOPES)}")
    setting = PluginSetting(key=key, type=kind, default=data.get("default") if isinstance(data, dict) else None,
                            label=label or key, help=help_text, minimum=minimum, maximum=maximum,
                            unit=f.text("unit", most=20) if numeric else "", pattern=pattern, max_length=max_length,
                            choices=tuple(choices), scope=scope, advanced=f.boolean("advanced"),
                            remakes=f.boolean("remakes"))
    if setting.remakes and (scope != "account" or kind == "secret"):
        # What a remake is made with is kept with what it made (its lineage), each account's.
        f.bad("remakes", "only an account's setting, and never a secret")
    if not f.has("default"):
        f.bad("default", "required")
    elif kind == "secret" and setting.default != "":
        f.bad("default", "empty (\"\") for a secret: a plug-in ships none")
    else:
        try:
            setting.check(setting.default)
        except ValueError as exc:
            f.bad("default", str(exc))
    return setting


def _work(data: Any, where: str, problems: list[str]) -> WorkSpec:
    f = _Fields(data, where, problems, ("title", "version", "media", "inputs", "batch", "outputs"))
    media = f.names("media", MEDIA) or MEDIA
    inputs = f.names("inputs", tuple(INPUTS))
    if "preview" in inputs and media != ("image",):
        f.bad("inputs", "preview is a photo's: its media is [\"image\"]")
    outputs: list[Output] = []
    for at, item in f.items("outputs", most=20):
        o = _Fields(item, at, problems, ("key", "kind", "label", "searchable", "empty"))
        key = o.text("key", required=True, most=40)
        if key and not _KEY.match(key):
            o.bad("key", "lowercase letters, digits and underscores, starting with a letter")
        kind = o.text("kind", required=True, most=20)
        if kind and kind not in OUTPUT_KINDS:
            o.bad("kind", f"one of {', '.join(OUTPUT_KINDS)} (protocol 1)")
        if key in [x.key for x in outputs]:
            problems.append(f"{at}: {key} twice")
        said = o.text("empty", most=EMPTY_MAX)
        empty = " ".join(said.split())
        if said and not plain(empty, EMPTY_MAX):
            o.bad("empty", f"plain text (no HTML), up to {EMPTY_MAX} characters")
        outputs.append(Output(key, kind, o.text("label", required=True, most=80), o.boolean("searchable"), empty))
    if not outputs:
        f.bad("outputs", "at least one")
    return WorkSpec(title=f.text("title", required=True, most=80), version=f.version("version"), media=media,
                    inputs=inputs, batch=f.whole("batch", 50, 1, 500), outputs=tuple(outputs))


def _run(data: Any, folder: Path | None, problems: list[str]) -> tuple[str, ...]:
    if not isinstance(data, list) or not 1 <= len(data) <= 32 \
            or not all(isinstance(a, str) and a and "\x00" not in a and len(a) <= 1000 for a in data):
        problems.append("run: its command, a list of 1 to 32 strings (the program first)")
        return ()
    program = data[0]
    if "/" in program:
        path = PurePosixPath(program)
        if path.is_absolute() or ".." in path.parts:
            problems.append("run[0]: a path in its folder, or a program's name (python3, node)")
        elif folder is not None and not (folder / program).is_file():
            problems.append(f"run[0]: {program} isn't in its folder")
    return tuple(data)


def parse(data: Any, folder: Path | None = None) -> Manifest:
    """A manifest from its JSON; ManifestError naming every problem. folder: where it is, to find its program in."""
    problems: list[str] = []
    f = _Fields(data, "", problems, (
        "id", "version", "protocol", "title", "description", "author", "homepage", "run", "acknowledgment",
        "credit", "downloads", "data_version", "internet", "memory_mb", "disk_mb", "check_settings", "settings",
        "work", "lookups"))
    plugin_id = f.text("id", required=True, most=40)
    if plugin_id and not _ID.match(plugin_id):
        f.bad("id", "lowercase letters, digits and dashes, starting with a letter (it's in URLs and commands)")
    protocol = f.data.get("protocol")
    if isinstance(protocol, bool) or protocol not in PROTOCOLS:
        f.bad("protocol", f"the plug-in protocol it speaks: {', '.join(map(str, PROTOCOLS))} (this Lumiverb's)")
    ack = _acknowledgment(f.data["acknowledgment"], "acknowledgment", problems) if f.has("acknowledgment") else None
    credit = None
    if f.has("credit"):
        c = _Fields(f.data["credit"], "credit", problems, ("text", "url", "license"))
        license_ = None
        if c.has("license"):
            lic = _Fields(c.data["license"], "credit.license", problems, ("text", "url"))
            license_ = Link(lic.text("text", required=True, most=100), lic.url("url", required=True))
        credit = Credit(c.text("text", required=True, most=100), c.url("url"), license_)
    downloads = []
    for at, item in f.items("downloads", most=20):
        d = _Fields(item, at, problems, ("url", "title", "about_mb"))
        downloads.append(Download(d.url("url", required=True), d.text("title", required=True, most=100),
                                  d.whole("about_mb", 0, 0, DISK_MB_MAX)))
    settings = [s for at, item in f.items("settings", most=50) if (s := _setting(item, at, problems)) is not None]
    keys = [s.key for s in settings]
    for key in sorted({k for k in keys if keys.count(k) > 1}):
        problems.append(f"settings: {key} twice")
    manifest = Manifest(
        plugin_id=plugin_id, version=f.version("version"), protocol=protocol if protocol in PROTOCOLS else 0,
        title=f.text("title", required=True, most=100), description=f.text("description", required=True, most=1000),
        run=_run(f.data.get("run"), folder, problems), author=f.text("author", most=100), homepage=f.url("homepage"),
        acknowledgment=ack, credit=credit, downloads=tuple(downloads),
        data_version=f.whole("data_version", 1, 1, 1_000_000), internet=f.boolean("internet"),
        memory_mb=f.whole("memory_mb", MEMORY_MB, 64, MEMORY_MB_MAX), disk_mb=f.whole("disk_mb", DISK_MB, 1, DISK_MB_MAX),
        check_settings=f.boolean("check_settings"), settings=tuple(settings),
        work=_work(f.data["work"], "work", problems) if f.has("work") else None,
        lookups=f.names("lookups", tuple(LOOKUPS)),
    )
    if problems:
        raise ManifestError(problems)
    return manifest


def read(folder: Path) -> Manifest:
    """The manifest in a plug-in's folder; ManifestError naming every problem."""
    path = folder / MANIFEST
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ManifestError([f"{MANIFEST}: can't be read in {folder} ({exc.strerror or exc})"]) from exc
    if len(raw) > _TOO_BIG:
        raise ManifestError([f"{MANIFEST}: over {_TOO_BIG // 1024} KB"])
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ManifestError([f"{MANIFEST}: not JSON ({exc})"]) from exc
    return parse(data, folder)
