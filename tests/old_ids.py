"""Where an old id of ADR-024 phase 1's first family (artifacts, lineage's
producers, job kinds) is still written in the code: src, the web's and the
apps' code, and their tests. tests/test_no_old_ids.py holds every match to
its allow-list.

A match is an old id as a whole string literal ("capture", 'scene-detect',
in SQL too), `producer.<old>`, `missing_<old>`, `redo_<old>`, an installed
plug-in's work id (`plugin_<id>`, `f"plugin_{...}"`) and an old id in a
path (`/producers/vision/`). The old ids are written out here, never read
from RENAMED, so the test stays when part 5 removes it.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

ARTIFACTS = ("probe", "capture", "proxy", "video_preview", "analysis_proxy", "scenes", "scene_vision", "vision", "ocr",
             "clip", "faces", "video_faces", "transcript", "location")
PRODUCERS = ("ffprobe", "exiftool", "preview", "analysis-proxy", "scene-detect", "scene-vision", "insightface",
             "insightface-video", "whisper")
KINDS = ("render",)
OLD = frozenset(ARTIFACTS + PRODUCERS + KINDS + ("previews",))
_WORD = "|".join(map(re.escape, sorted(OLD, key=len, reverse=True)))
_ART = "|".join(map(re.escape, sorted(ARTIFACTS + KINDS, key=len, reverse=True)))
# Inside a string: what's built from an old id.
_BUILT = re.compile(rf"\b(producer\.(?:{_ART})\b|missing_(?:{_ART})\b|redo_(?:{_ART})\b"
                    rf"|(?<!src)/producers/(?:{_WORD})(?=[/?\"'`]|$))")  # a route, not a module's path
_SQL = re.compile(rf"'({_WORD})'")
_QUOTED = re.compile(rf"""(["'`])({_WORD})\1""")
_FILES = {".py": "python", ".ts": "script", ".tsx": "script", ".js": "script", ".swift": "script"}
_SKIP = {"node_modules", "__pycache__", ".venv", "dist", "build", "DerivedData", ".build", "fixtures"}
ROOTS = ("src", "clients", "tests")


def plugin_ids() -> frozenset[str]:
    """Every plug-in in the repo (Lumiverb's own, its example, the tests'), each dash an underscore as its work's
    id had it."""
    ids = set()
    for manifest in REPO.glob("**/lumiverb-plugin.json"):
        if not _SKIP & set(manifest.relative_to(REPO).parts):
            ids.add(json.loads(manifest.read_text())["id"].replace("-", "_"))
    return frozenset(ids)


def _plugin_work(ids: frozenset[str]) -> re.Pattern:
    names = "|".join(map(re.escape, sorted(ids, key=len, reverse=True)))
    return re.compile(rf"""(?<![\w.])(plugin_(?:{names})\b|plugin_\{{|["']plugin_["']\s*\+)""")


# The allow-list names every word it allows, and the test's own samples are what it looks for.
NOT_LOOKED_IN = (Path(__file__).with_name("old_ids_allowed.py"), Path(__file__).with_name("test_no_old_ids.py"))


def files() -> Iterator[Path]:
    for root in ROOTS:
        for path in sorted((REPO / root).rglob("*")):
            if path.suffix in _FILES and path.is_file() and not _SKIP & set(path.relative_to(REPO).parts) \
                    and path not in NOT_LOOKED_IN:
                yield path


def _python(source: str, work: re.Pattern) -> Iterator[tuple[int, str]]:
    tree = ast.parse(source)
    inside = {id(v) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr) for v in node.values}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in inside:
            value = node.value
            if value in OLD or value == "plugin_":  # "plugin_" + its id
                yield node.lineno, value
                continue
            for pattern in (_SQL, _BUILT, work):
                for m in pattern.finditer(value):
                    yield node.lineno, m.group(1)
        elif isinstance(node, ast.JoinedStr):  # an f-string: its parts, and plugin_{...}
            parts = "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
            for pattern in (_BUILT, work):
                for m in pattern.finditer(parts):
                    yield node.lineno, m.group(1)


def _script(source: str, work: re.Pattern) -> Iterator[tuple[int, str]]:
    for number, line in enumerate(source.split("\n"), 1):
        code = line.strip()
        if code.startswith(("//", "*", "/*")):
            continue  # a comment
        for m in _QUOTED.finditer(line):
            yield number, m.group(2)
        for pattern in (_BUILT, work):
            for m in pattern.finditer(line):
                yield number, m.group(1)


def matches() -> Iterator[tuple[str, int, str]]:
    """(path, line, what) for every match, the path from the repo's root."""
    work = _plugin_work(plugin_ids())
    for path in files():
        source = path.read_text(encoding="utf-8")
        found = _python(source, work) if _FILES[path.suffix] == "python" else _script(source, work)
        for line, what in sorted(set(found)):
            yield str(path.relative_to(REPO)), line, what
