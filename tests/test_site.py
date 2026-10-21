"""lumiverb.io's front page (site/, served as it is: no build) held to what it
says and what it loads. Its install commands are the README's, the formats it
lists are the ones a scan takes, its links into the repository lead somewhere,
and it loads nothing from another host (no fonts, scripts or counters: a
privacy-first program's page asks nobody else for anything).

Read as the browser reads it: the page's tags and their text."""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.fast

REPO = Path(__file__).resolve().parents[1]
SITE = REPO / "site"
GITHUB = "https://github.com/bubbafat/lumiverb"
HOME = "https://lumiverb.io/"


class _Page(HTMLParser):
    """Every tag with its attributes; each command's text (`.cmd`'s code); the text of each element by id."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str]]] = []
        self.commands: list[str] = []
        self.by_id: dict[str, str] = {}
        self._open: list[tuple[str, str | None, bool]] = []  # tag, id, inside a command
        self._text: dict[int, list[str]] = {}

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        self.tags.append((tag, a))
        if tag in {"meta", "link", "img", "br", "source", "input"}:
            return
        in_cmd = "cmd" in a.get("class", "").split() or any(c for _, _, c in self._open)
        self._open.append((tag, a.get("id"), in_cmd))
        self._text[len(self._open)] = []

    def handle_endtag(self, tag):
        while self._open:
            depth = len(self._open)
            t, ident, in_cmd = self._open.pop()
            text = "".join(self._text.pop(depth, []))
            if self._open:
                self._text[len(self._open)].append(text)
            if ident:
                self.by_id[ident] = text
            if t == "code" and in_cmd:
                self.commands.append(text)
            if t == tag:
                return

    def handle_data(self, data):
        if self._open:
            self._text[len(self._open)].append(data)


def _page() -> _Page:
    p = _Page()
    p.feed((SITE / "index.html").read_text(encoding="utf-8"))
    return p


def _named(page: _Page) -> list[str]:
    """Every address the page names: what it loads and where it links."""
    return [a[k] for _, a in page.tags for k in ("src", "href") if k in a]


def test_its_install_commands_are_the_readmes():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    commands = _page().commands
    assert len(commands) >= 6
    for command in commands:
        assert unescape(command) in readme, f"not in the README as it's typed there: {command!r}"


def test_the_formats_it_lists_are_the_ones_a_scan_takes():
    from src.shared.file_extensions import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS

    spelled = {"JPEG": {".jpg", ".jpeg"}, "TIFF": {".tif", ".tiff"}}

    def listed(ident: str) -> set[str]:
        words = re.findall(r"\b(?:[A-Z0-9]{2,5}|WebP)\b", _page().by_id[ident])
        return set().union(*(spelled.get(w, {"." + w.lower()}) for w in words))

    assert listed("formats-photos") == IMAGE_EXTENSIONS
    assert listed("formats-videos") == VIDEO_EXTENSIONS


def test_it_loads_nothing_from_another_host():
    page = _page()
    for tag, a in page.tags:
        loads = (
            a.get("src")
            if tag in {"img", "script", "source", "iframe"}
            else (a.get("href") if tag == "link" and a.get("rel") != "canonical" else None)
        )
        if loads:
            assert not urlsplit(loads).netloc, f"<{tag}> loads {loads} from another host"
    [policy] = [
        a["content"]
        for t, a in page.tags
        if t == "meta" and a.get("http-equiv") == "Content-Security-Policy"
    ]
    assert "default-src 'self'" in policy and "http" not in policy
    for asset in ("style.css", "site.js"):
        text = (SITE / asset).read_text(encoding="utf-8")
        assert not re.search(r"https?://|@import", text), f"{asset} reaches for another host"


def test_every_file_it_names_is_there_and_every_file_there_is_named():
    named = set()
    for address in _named(_page()) + [
        a["content"] for _, a in _page().tags if a.get("property") == "og:image"
    ]:
        if address.startswith(HOME):
            address = address[len(HOME) :]
        parts = urlsplit(address)
        if parts.scheme or parts.netloc or not parts.path or parts.path == "/":
            continue
        named.add(parts.path)
        assert (SITE / parts.path).is_file(), f"the page names {address}, which isn't in site/"
    for f in SITE.rglob("*"):
        if f.is_file():
            assert f.relative_to(SITE).as_posix() in named | {"index.html"}, (
                f"site/{f.name} isn't used"
            )


def test_its_pictures_are_the_size_it_says():
    from PIL import Image

    for tag, a in _page().tags:
        if tag == "img":
            with Image.open(SITE / a["src"]) as im:
                assert (int(a["width"]), int(a["height"])) == im.size, a["src"]
            assert a.get("alt"), f"{a['src']} says nothing to someone who can't see it"


def test_its_links_into_the_repository_lead_somewhere():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    # GitHub's anchor for a heading: lower case, punctuation dropped, spaces as hyphens.
    anchors = {
        re.sub(r"[^\w\- ]", "", h.strip().lower()).replace(" ", "-")
        for h in re.findall(r"^#{1,6} (.+)$", readme, re.M)
    }
    page = _page()
    ids = {a["id"] for _, a in page.tags if "id" in a}
    for address in _named(page):
        if address.startswith(GITHUB + "/blob/main/"):
            path = address[len(GITHUB + "/blob/main/") :].split("#")[0]
            assert (REPO / path).is_file(), f"{address}: no {path} in the repository"
        elif address.startswith(GITHUB + "#"):
            assert address.split("#", 1)[1] in anchors, f"{address}: no such heading in the README"
        elif address.startswith("#"):
            assert address[1:] in ids, f"{address}: nothing on the page has that id"
