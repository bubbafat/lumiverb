"""docs.lumiverb.io: the user guide (docs/guide/*.md, in the order docs/guide/nav.txt gives) built into static
pages in docs-site/, which nginx serves as they are (scripts/deploy-site.sh --folder docs-site).

The built pages are kept in the repository, as docs/data-names.md is: tests/test_docs_site.py builds the guide
again and fails until docs-site/ is what this writes. After changing a page or the theme:

    uv run --no-sync python scripts/build_docs.py

The same every time (no dates, nothing random), so a page changes only when its source or the theme does.
Nothing a page loads comes from another host, as on lumiverb.io.
"""

from __future__ import annotations

import html
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from string import Template

from markdown_it import MarkdownIt
from markdown_it.token import Token

REPO = Path(__file__).resolve().parents[1]
GUIDE = REPO / "docs" / "guide"
THEME = Path(__file__).resolve().parent / "docs_theme"
OUT = REPO / "docs-site"
GITHUB = "https://github.com/bubbafat/lumiverb"
HOME = "https://docs.lumiverb.io/"
# Copied as they are, beside the pages.
ASSETS = {
    "docs.css": THEME / "docs.css",
    "docs.js": THEME / "docs.js",
    "favicon.svg": REPO / "site" / "favicon.svg",
}


@dataclass
class Page:
    source: str  # its file in docs/guide
    title: str = ""
    description: str = ""
    body: str = ""
    sections: list[tuple[str, str]] = field(default_factory=list)  # (id, title) of each ##

    @property
    def out(self) -> str:
        return self.source.removesuffix(".md") + ".html"


@dataclass
class Link:
    title: str
    url: str


def read_nav() -> list[tuple[str, list[Page | Link]]]:
    """nav.txt: a section's name at the start of a line, its entries indented below it, each a page's file or
    `Title | URL`. A line starting with # is a note."""
    sections: list[tuple[str, list[Page | Link]]] = []
    for n, line in enumerate((GUIDE / "nav.txt").read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            sections.append((line.strip(), []))
            continue
        if not sections:
            raise SystemExit(f"nav.txt:{n}: an entry before any section")
        entry = line.strip()
        if "|" in entry:
            title, url = (s.strip() for s in entry.split("|", 1))
            sections[-1][1].append(Link(title, url))
        elif (GUIDE / entry).is_file():
            sections[-1][1].append(Page(entry))
        else:
            raise SystemExit(f"nav.txt:{n}: no docs/guide/{entry}")
    return sections


def slug(text: str) -> str:
    """A heading's anchor as GitHub makes it: lower case, punctuation dropped, spaces as hyphens."""
    return re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")


def plain(inline: Token) -> str:
    return "".join(c.content for c in inline.children or [] if c.type in {"text", "code_inline"})


class Renderer:
    def __init__(self, pages: dict[str, Page]) -> None:
        self.pages = pages
        self.md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
        rules = self.md.renderer.rules
        rules["fence"] = self._fence
        rules["table_open"] = lambda *a: '<div class="table"><table>\n'
        rules["table_close"] = lambda *a: "</table></div>\n"
        rules["heading_close"] = self._heading_close

    def _fence(self, tokens, idx, options, env):
        tok = tokens[idx]
        lang = tok.info.strip().split(" ")[0]
        code = html.escape(tok.content.rstrip("\n"), quote=False)
        if lang == "bash":
            return f'<div class="cmd"><pre><code>{code}</code></pre></div>\n'
        cls = f' class="language-{html.escape(lang)}"' if lang else ""
        return f"<pre><code{cls}>{code}</code></pre>\n"

    def _heading_close(self, tokens, idx, options, env):
        tok = tokens[idx]
        ident = tokens[idx - 2].attrGet("id") if tok.tag in {"h2", "h3"} else None
        anchor = (
            f'<a class="anchor" href="#{ident}" aria-label="Link to this section">#</a>'
            if ident
            else ""
        )
        return f"{anchor}</{tok.tag}>\n"

    def href(self, page: Page, url: str) -> str:
        """A link as it's written in the guide's Markdown, as it's served: another page's .md as its .html, a file
        elsewhere in the repository on GitHub. Anything else that names a .md is a mistake, stopped here."""
        if re.match(r"^[a-z][a-z0-9+.-]*:", url) or url.startswith("#"):
            return url
        path, _, anchor = url.partition("#")
        anchor = f"#{anchor}" if anchor else ""
        if path in self.pages:
            return self.pages[path].out + anchor
        target = (GUIDE / path).resolve()
        if target.is_relative_to(REPO) and target.exists():
            rel = target.relative_to(REPO).as_posix()
            return f"{GITHUB}/{'tree' if target.is_dir() else 'blob'}/main/{rel}{anchor}"
        raise SystemExit(
            f"docs/guide/{page.source}: a link to {url}, which isn't a page or a file in the repository"
        )

    def render(self, page: Page) -> None:
        tokens = self.md.parse((GUIDE / page.source).read_text(encoding="utf-8"))
        seen: dict[str, int] = {}
        for i, tok in enumerate(tokens):
            if tok.type == "heading_open":
                text = plain(tokens[i + 1])
                if tok.tag == "h1":
                    if page.title:
                        raise SystemExit(f"docs/guide/{page.source}: more than one # title")
                    page.title = text
                    continue
                base = slug(text)
                n = seen.get(base, 0)
                seen[base] = n + 1
                ident = base if n == 0 else f"{base}-{n}"
                tok.attrSet("id", ident)
                if tok.tag == "h2":
                    page.sections.append((ident, text))
            elif tok.type == "paragraph_open" and not page.description:
                page.description = re.sub(r"\s+", " ", plain(tokens[i + 1])).strip()
            elif tok.type == "inline":
                for child in tok.children or []:
                    if child.type == "link_open":
                        child.attrSet("href", self.href(page, child.attrGet("href") or ""))
        if not page.title:
            raise SystemExit(f"docs/guide/{page.source}: no # title on its first line")
        page.body = self.md.renderer.render(tokens, self.md.options, {})


def nav_html(sections, current: Page) -> str:
    out = []
    for name, entries in sections:
        out.append(f'<p class="nav-section">{html.escape(name)}</p>\n<ul>')
        for e in entries:
            if isinstance(e, Page):
                here = ' aria-current="page"' if e is current else ""
                out.append(f'<li><a href="{e.out}"{here}>{html.escape(e.title)}</a></li>')
            else:
                out.append(
                    f'<li><a href="{html.escape(e.url)}">{html.escape(e.title)} <span aria-hidden="true">↗</span></a></li>'
                )
        out.append("</ul>")
    return "\n".join(out)


def build(out: Path = OUT) -> list[str]:
    """Writes the site into `out` (emptied first); returns the files written."""
    sections = read_nav()
    order = [e for _, entries in sections for e in entries if isinstance(e, Page)]
    pages = {p.source: p for p in order}
    missing = sorted(f.name for f in GUIDE.glob("*.md") if f.name not in pages)
    if missing:
        raise SystemExit(f"docs/guide: not in nav.txt: {', '.join(missing)}")
    if "index.md" not in pages:
        raise SystemExit("docs/guide: no index.md")
    renderer = Renderer(pages)
    for page in order:
        renderer.render(page)

    template = Template((THEME / "page.html").read_text(encoding="utf-8"))
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    written = []
    for i, page in enumerate(order):
        prev_page = order[i - 1] if i > 0 else None
        next_page = order[i + 1] if i + 1 < len(order) else None
        pager = "".join(
            f'<a class="{cls}" href="{p.out}"><span>{label}</span>{html.escape(p.title)}</a>'
            for cls, label, p in (("prev", "Previous", prev_page), ("next", "Next", next_page))
            if p
        )
        toc = "".join(
            f'<li><a href="#{ident}">{html.escape(t)}</a></li>' for ident, t in page.sections
        )
        text = template.substitute(
            title=html.escape(
                "Lumiverb docs" if page.source == "index.md" else f"{page.title} · Lumiverb docs"
            ),
            description=html.escape(page.description[:200], quote=True),
            canonical=HOME + ("" if page.source == "index.md" else page.out),
            nav=nav_html(sections, page),
            body=page.body,
            toc=f'<nav class="toc" aria-label="On this page"><p>On this page</p><ul>{toc}</ul></nav>'
            if toc
            else "",
            pager=pager,
            source=f"{GITHUB}/blob/main/docs/guide/{page.source}",
        )
        (out / page.out).write_text(text, encoding="utf-8")
        written.append(page.out)
    for name, src in ASSETS.items():
        shutil.copyfile(src, out / name)
        written.append(name)
    return sorted(written)


if __name__ == "__main__":
    files = build(Path(sys.argv[1]) if len(sys.argv) > 1 else OUT)
    print(f"docs-site: {len(files)} files")
