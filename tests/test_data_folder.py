"""Every name Lumiverb keeps in its data folder (DATA_DIR) is in one list,
src/server/data_folder.py, so a folder chosen for previews never takes one
(src/server/storage/move.py check_name). Held to the code: each name the
code joins onto DATA_DIR, and each the install scripts write there, is
listed, and each listed is one of them. A new folder or file in DATA_DIR
joins the list, or this fails naming it."""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path, PurePath

import pytest

from src.server import data_folder

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
# Plug-in programs run with a folder of their own (DATA_DIR/plugins/<id>/data), never DATA_DIR.
NOT_LUMIVERBS = ("src/own_plugins/", "src/plugin_examples/")
# How the code names DATA_DIR: these names, these attributes, and what's made of them.
DATA_NAMES = {"data_dir", "data"}
DATA_ATTRS = {"data_dir", "_data_dir"}
# Joins whose name is worked out rather than one of Lumiverb's own: where, and what it is.
WORKED_OUT = {
    ("src/server/storage/move.py", "_trusted"): "the first folder of the one an admin chose, by its own name",
}
# The install scripts' DATA_DIR/<name>, however it's spelled: ${DATA_DIR}/x, $DATA_DIR/x, "$DATA_DIR"/x.
SCRIPT_JOIN = re.compile(r'\$\{?DATA_DIR\}?"?/([A-Za-z0-9._-]+)')


def _says_data_dir(node: ast.AST) -> bool:
    return any((isinstance(n, ast.Name) and n.id in DATA_NAMES)
               or (isinstance(n, ast.Attribute) and n.attr in DATA_ATTRS)
               or (isinstance(n, ast.Constant) and n.value == "DATA_DIR") for n in ast.walk(node))


def _is_data_dir(node: ast.AST) -> bool:
    """DATA_DIR as the code has it: data_dir, data, settings.data_dir, Path(...) of one, data_dir(env), and those
    made absolute."""
    if isinstance(node, ast.Name):
        return node.id in DATA_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr in DATA_ATTRS
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            return func.id == "data_dir" or (func.id == "Path" and any(_says_data_dir(a) for a in node.args))
        if isinstance(func, ast.Attribute) and func.attr in ("absolute", "resolve", "expanduser"):
            return _is_data_dir(func.value)
    return False


def _joins(tree: ast.AST) -> list[tuple[str, int, ast.AST]]:
    """Each (function, line, what's joined) onto DATA_DIR: data / x, data.joinpath(x), os.path.join(data, x),
    f"{data}/x"."""
    found: list[tuple[str, int, ast.AST]] = []

    def visit(node: ast.AST, function: str) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            function = node.name
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) and _is_data_dir(node.left):
            found.append((function, node.lineno, node.right))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
            if node.func.attr == "joinpath" and _is_data_dir(node.func.value):
                found.append((function, node.lineno, node.args[0]))
            elif (node.func.attr == "join" and ast.unparse(node.func.value) == "os.path" and len(node.args) > 1
                  and _is_data_dir(node.args[0])):
                found.append((function, node.lineno, node.args[1]))
        elif isinstance(node, ast.JoinedStr):
            for a, b in zip(node.values, node.values[1:], strict=False):
                if (isinstance(a, ast.FormattedValue) and _is_data_dir(a.value) and isinstance(b, ast.Constant)
                        and str(b.value).startswith("/")):
                    found.append((function, node.lineno, ast.Constant(str(b.value)[1:])))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, "")
    return found


def _first(value: object) -> str | None:
    """The name in DATA_DIR a joined str or path is under."""
    if isinstance(value, str | PurePath) and str(value):
        return PurePath(value).parts[0]
    return None


def _named_in_code() -> tuple[dict[str, list[str]], list[str]]:
    """Each name the code joins onto DATA_DIR, with where; and the joins it can't tell the name of."""
    names: dict[str, list[str]] = {}
    unknown: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if "node_modules" in path.parts or rel.startswith(NOT_LUMIVERBS):
            continue
        module = None
        for function, line, joined in _joins(ast.parse(path.read_text())):
            name = joined.value if isinstance(joined, ast.Constant) else None
            if name is None and isinstance(joined, ast.Name | ast.Attribute):
                # A module's constant (STATE_FILE, previews_location.FILE, TLS_DIR...): its value.
                module = module or importlib.import_module(rel.removesuffix(".py").removesuffix("/__init__")
                                                           .replace("/", "."))
                try:
                    name = eval(compile(ast.Expression(joined), rel, "eval"), vars(module))  # noqa: S307
                except (NameError, AttributeError):
                    name = None
            if (first := _first(name)) is not None:
                names.setdefault(first, []).append(f"{rel}:{line}")
            elif (rel, function) not in WORKED_OUT:
                unknown.append(f"{rel}:{line} ({function}): {ast.unparse(joined)}")
    return names, unknown


def _named_in_scripts() -> dict[str, list[str]]:
    names: dict[str, list[str]] = {}
    for path in sorted((ROOT / "scripts").rglob("*.sh")):
        for line_no, line in enumerate(path.read_text().splitlines(), 1):
            for name in SCRIPT_JOIN.findall(line):
                names.setdefault(name, []).append(f"{path.relative_to(ROOT).as_posix()}:{line_no}")
    return names


def test_each_name_lumiverb_keeps_in_its_data_folder_is_listed_and_each_listed_is_one():
    in_code, unknown = _named_in_code()
    in_scripts = _named_in_scripts()
    assert not unknown, ("joined onto DATA_DIR with a name this test can't tell: use a literal or a module's "
                         f"constant, or say why in WORKED_OUT: {unknown}")
    made = {name: in_code.get(name, []) + in_scripts.get(name, []) for name in {*in_code, *in_scripts}}
    unlisted = {name: made[name] for name in sorted(made) if data_folder.kept(name) is None}
    assert not unlisted, f"made in DATA_DIR but not in src/server/data_folder.py NAMES: {unlisted}"
    assert sorted(set(data_folder.NAMES) - set(made)) == [], "listed, but nothing makes or reads it in DATA_DIR"


def test_it_finds_what_each_kind_of_join_names():
    """The walk itself: each way the code joins onto DATA_DIR is seen, and a path that isn't DATA_DIR isn't."""
    code = '''
import os
from pathlib import Path
NAME = "named.json"
def f(data_dir, data, settings, home):
    data_dir / "a"
    data / NAME
    Path(settings.data_dir) / "b" / "deeper"
    Path(get_settings().data_dir).absolute() / "c"
    data_dir(env) / "d.json"
    data.joinpath("e")
    os.path.join(data_dir, "f")
    f"{data / 'g'}/"
    f"{data}/h/i"
    home / "not"
    data / name
'''
    joined = [(function, ast.unparse(j)) for function, _, j in _joins(ast.parse(code))]
    assert joined == [("f", "'a'"), ("f", "NAME"), ("f", "'b'"), ("f", "'c'"), ("f", "'d.json'"), ("f", "'e'"),
                      ("f", "'f'"), ("f", "'g'"), ("f", "'h/i'"), ("f", "name")]
    assert SCRIPT_JOIN.findall('svc_folder "$DATA_DIR"/tmp ${DATA_DIR}/cache/nvidia $DATA_DIR/plugins x/DATA_DIR/no') \
        == ["tmp", "cache", "plugins"]


def test_a_name_is_matched_whatever_its_case():
    assert data_folder.kept("Models") == data_folder.NAMES["models"]
    assert data_folder.kept("STORAGE.JSON") == data_folder.NAMES["storage.json"]
    assert data_folder.kept("Lumiverb previews") is None
    assert data_folder.kept("ten_abc") is None, "the previews' own account folders aren't in the list"
