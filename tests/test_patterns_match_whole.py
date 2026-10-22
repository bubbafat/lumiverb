"""A pattern that ends in `$` is matched whole: fullmatch, never match or search.

In Python's re, `$` also matches just before a final newline, so `^[a-z]+$`
with match() takes "foo\\n": a plug-in's id with a newline at its end got
through to folder names, sockets and SQL text. Every check in src/ reads
the whole value (fullmatch, or `\\Z` where a suffix is searched for); the
first test reads the code so a new one does too.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _constant(node: ast.expr, strings: dict[str, str]) -> str | None:
    """A pattern's text when the code spells it out (a literal, literals joined, or a name for one)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return strings.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        right = _constant(node.right, strings)
        return right if right is not None and right.endswith("$") else None  # only its end matters here
    if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[-1], ast.Constant):
        return str(node.values[-1].value)
    return None


def _ends_anchored(pattern: str | None) -> bool:
    return pattern is not None and pattern.endswith("$") and not pattern.endswith("\\$")


def _is_re(node: ast.expr, name: str) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == name and isinstance(node.value, ast.Name)
            and node.value.id == "re")


def _strings(tree: ast.Module) -> dict[str, str]:
    """The names a module gives a string literal (PATTERN = r"^…$")."""
    return {t.id: n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
            for t in n.targets if isinstance(t, ast.Name)
            and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)}


def matched_partly(paths: list[Path]) -> list[str]:
    trees = {path: ast.parse(path.read_text(), str(path)) for path in paths}
    # Each module's compiled patterns that end in $, by name: its own (_ID.match) and another's (mod.NAME.match).
    anchored: dict[Path, set[str]] = {}
    for path, tree in trees.items():
        strings = _strings(tree)
        anchored[path] = {t.id for node in ast.walk(tree) if isinstance(node, ast.Assign)
                          and isinstance(node.value, ast.Call) and _is_re(node.value.func, "compile")
                          and node.value.args and _ends_anchored(_constant(node.value.args[0], strings))
                          for t in node.targets if isinstance(t, ast.Name)}
    anywhere = set().union(*anchored.values())
    found = []
    for path, tree in trees.items():
        strings = _strings(tree)
        # Its own, and those it imports (from src.producers.names import NAME).
        own = anchored[path] | ({a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                                 for a in n.names} & anywhere)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("match", "search")):
                continue
            target = node.func.value
            if isinstance(target, ast.Name) and target.id == "re":
                partly = bool(node.args) and _ends_anchored(_constant(node.args[0], strings))
            elif isinstance(target, ast.Name):
                partly = target.id in own
            else:
                partly = isinstance(target, ast.Attribute) and target.attr in anywhere
            if partly:
                found.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}: {ast.unparse(node)[:100]}")
    return found


def test_every_pattern_ending_in_dollar_is_matched_whole() -> None:
    found = matched_partly(sorted((ROOT / "src").rglob("*.py")))
    assert not found, "Use fullmatch (or \\Z): `$` matches before a final newline too\n" + "\n".join(found)


def test_the_check_finds_what_it_is_for(tmp_path) -> None:
    source = tmp_path / "x.py"
    source.write_text(
        "import re\n"
        "PATTERN = r'^[a-z]+$'\n"
        "_ID = re.compile(r'^[a-z][a-z0-9-]{0,39}$')\n"
        "_NAME = re.compile(PATTERN)\n"
        "_PREFIX = re.compile(r'^ast_')\n"
        "def ok(x):\n"
        "    return _ID.fullmatch(x) and _PREFIX.match(x) and re.match(r'^a', x)\n"
        "def not_ok(x, mod):\n"
        "    return _ID.match(x) or _NAME.search(x) or mod._ID.match(x) or re.match(r'^[a-z]+$', x)\n"
    )
    global ROOT
    was, ROOT = ROOT, tmp_path
    try:
        found = [line.split(": ", 1)[1] for line in matched_partly([source])]
    finally:
        ROOT = was
    assert found == ["_ID.match(x)", "_NAME.search(x)", "mod._ID.match(x)", "re.match('^[a-z]+$', x)"]


# What each check takes, with a newline at the end: nothing.


def test_a_plug_ins_id_and_a_name_end_where_they_end() -> None:
    from src.plugins import version_key
    from src.producers.names import is_reserved, sql_literal, sql_spelling

    assert sql_literal("geonames") == "'geonames'"
    for bad in ("geonames\n", "geo'names", "geonames'; --", ""):
        with pytest.raises(ValueError):
            sql_literal(bad)
    with pytest.raises(ValueError):
        sql_spelling("lumiverb.preview\n")
    with pytest.raises(ValueError):
        sql_spelling("hello\n")
    assert sql_spelling("hello-world") == "hello_world" and not is_reserved("hello")
    assert version_key("1.2\n") is None and version_key("1.2") == (1, 2)


def test_what_reaches_commands_and_databases_ends_where_it_ends() -> None:
    from src.processing.video import ffmpeg_run
    from src.server import database
    from src.server.plugin_lookups import _text
    from src.shared import content_hash

    sha = "sha256:" + "a" * 64
    assert content_hash.well_formed(sha) and content_hash.ours(sha)
    assert not content_hash.well_formed(sha + "\n") and not content_hash.ours(sha + "\n")
    assert ffmpeg_run.decoded_by("vaapi") == "vaapi"
    with pytest.raises(ValueError):
        ffmpeg_run.decoded_by("vaapi\n")  # a decoder's name goes into ffmpeg's command
    for call in (database.provision_tenant_database, database.deprovision_tenant_database):
        with pytest.raises(ValueError, match="Invalid tenant_id"):
            call("ten_a\n")  # a database's name
    assert _text("Zürich", most=100) == "Zürich"
    with pytest.raises(ValueError):
        _text("Zürich\n", most=100)  # a place's name a plug-in sends: one line of plain text
