"""docs.lumiverb.io: the user guide (docs/guide/) and its built pages
(docs-site/, served as they are by scripts/deploy-site.sh --folder docs-site).

The built pages are what scripts/build_docs.py makes of the guide now (the
same bytes), every link leads somewhere (a page, a heading on it, a file in
the repository), nothing is loaded from another host, and every command the
guide gives for `lumiverb` or `lumiverb-server` is one their own parsers take:
the command, its subcommands and its options, as written."""

from __future__ import annotations

import argparse
import importlib.util
import re
import shlex
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.fast

REPO = Path(__file__).resolve().parents[1]
GUIDE = REPO / "docs" / "guide"
BUILT = REPO / "docs-site"
GITHUB = "https://github.com/bubbafat/lumiverb"
REBUILD = "uv run --no-sync python scripts/build_docs.py"


def _builder():
    spec = importlib.util.spec_from_file_location("build_docs", REPO / "scripts" / "build_docs.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look their module up while it loads
    spec.loader.exec_module(module)
    return module


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, {k: v or "" for k, v in attrs}))


def _tags(page: Path) -> list[tuple[str, dict[str, str]]]:
    p = _Tags()
    p.feed(page.read_text(encoding="utf-8"))
    return p.tags


def _pages() -> list[Path]:
    return sorted(BUILT.glob("*.html"))


def test_the_built_pages_are_the_guide_as_it_is_now(tmp_path):
    written = _builder().build(tmp_path / "built")
    built = sorted(f.relative_to(BUILT).as_posix() for f in BUILT.rglob("*") if f.is_file())
    assert built == written, f"docs-site/ isn't the guide's files: {REBUILD}"
    for name in written:
        assert (BUILT / name).read_bytes() == (tmp_path / "built" / name).read_bytes(), (
            f"docs-site/{name} isn't what the guide makes now: {REBUILD}"
        )


def test_every_link_leads_somewhere():
    ids = {p.name: {a["id"] for _, a in _tags(p) if "id" in a} for p in _pages()}
    for page in _pages():
        for tag, a in _tags(page):
            if tag != "a" or "href" not in a:
                continue
            href = a["href"]
            parts = urlsplit(href)
            if href.startswith(GITHUB + "/blob/main/") or href.startswith(GITHUB + "/tree/main/"):
                path = parts.path.split("/main/", 1)[1]
                assert (REPO / path).exists(), f"{page.name}: {href}: no {path} in the repository"
            elif not parts.scheme and not parts.netloc:
                target = parts.path or page.name
                assert target in ids, f"{page.name}: {href}: no such page"
                if parts.fragment:
                    assert parts.fragment in ids[target], (
                        f"{page.name}: {href}: no such heading on {target}"
                    )


def test_it_loads_nothing_from_another_host():
    for page in _pages():
        for tag, a in _tags(page):
            loads = (
                a.get("src")
                if tag in {"img", "script", "source", "iframe"}
                else (a.get("href") if tag == "link" and a.get("rel") != "canonical" else None)
            )
            if loads:
                assert not urlsplit(loads).netloc, (
                    f"{page.name}: <{tag}> loads {loads} from another host"
                )
    for asset in ("docs.css", "docs.js"):
        assert not re.search(r"https?://|@import", (BUILT / asset).read_text()), (
            f"{asset} reaches for another host"
        )


def _commands() -> list[tuple[str, str, list[str]]]:
    """(page, program, its arguments) for each lumiverb or lumiverb-server command in the guide's bash blocks."""
    found = []
    for md in sorted(GUIDE.glob("*.md")):
        text = md.read_text(encoding="utf-8")
        blocks = re.findall(r"^```bash\n(.*?)^```", text, re.S | re.M)
        # And each one written in a sentence or a table: `lumiverb library list`.
        prose = re.sub(r"^```.*?^```", "", text, flags=re.S | re.M)
        blocks += re.findall(r"`((?:lumiverb|lumiverb-server) [^`]+)`", prose)
        for block in blocks:
            lexer = shlex.shlex(block.replace("\\\n", " "), posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            words, segments = list(lexer), [[]]
            for w in words:
                if w in {"&&", "||", "|", ";", "\n"}:
                    segments.append([])
                else:
                    segments[-1].append(w)
            for seg in segments:
                # The program: past sudo and its options (-u USER, -H) and any VAR=value.
                i = 0
                while i < len(seg) and (
                    seg[i] in {"sudo", "-H", "-E"} or re.match(r"^[A-Z_]+=", seg[i])
                ):
                    i += 1
                    if i < len(seg) and seg[i] == "-u":
                        i += 2
                if i < len(seg) and Path(seg[i]).name in {"lumiverb", "lumiverb-server"}:
                    found.append((md.name, Path(seg[i]).name, seg[i + 1 :]))
    return found


def test_the_guide_gives_commands():
    programs = {p for _, p, _ in _commands()}
    assert programs == {"lumiverb", "lumiverb-server"}, programs


@pytest.mark.parametrize(
    "page,args",
    [(p, a) for p, prog, a in _commands() if prog == "lumiverb-server"],
    ids=lambda v: v if isinstance(v, str) else " ".join(v),
)
def test_every_lumiverb_server_command_is_one_it_takes(page, args, monkeypatch, capsys):
    from src.server.lifecycle import cli

    class Parsed(Exception):  # noqa: N818 (not an error: how a parse that worked stops before running)
        pass

    parse = argparse.ArgumentParser.parse_args

    def parsed_only(self, *a, **k):
        raise Parsed(parse(self, *a, **k))

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", parsed_only)
    try:
        cli.main(args)
    except Parsed:
        return
    except SystemExit:
        pass
    pytest.fail(f"{page}: lumiverb-server {' '.join(args)}: {capsys.readouterr().err.strip()}")


@pytest.mark.parametrize(
    "page,args",
    [(p, a) for p, prog, a in _commands() if prog == "lumiverb"],
    ids=lambda v: v if isinstance(v, str) else " ".join(v),
)
def test_every_lumiverb_command_is_one_it_takes(page, args):
    from typer.testing import CliRunner

    from src.client.cli.main import app

    # --help last: the command and every option before it are parsed, and nothing runs.
    result = CliRunner().invoke(app, [*args, "--help"])
    assert result.exit_code == 0, (
        f"{page}: lumiverb {' '.join(args)}: {result.output.strip()[-400:]}"
    )
