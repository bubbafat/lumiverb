"""The CLI prints what the server sends as text, never as Rich markup.

Rich reads square brackets in what it prints as markup: a library named
`x[/b]` stopped a command with a MarkupError, and `[link=…]` in a name made a
terminal link. So every value the CLI (src/client) and a scan
(src/processing) put into what Rich prints, a table's cells and title and a
progress bar's description too, is passed through rich.markup.escape (or
scan.py's `_shown`, for paths) unless it's Lumiverb's own text or a number.

The first test reads the code. A value put into a console's print, log,
rule, status or input, a table's row or title, or a progress description
passes when it's escaped, a constant, a number (a number format such as
`:,`, `:d` or `.1f`, or a count: len, int, round, …), a table (its cells are
checked where they're added), or a name or function of the same file whose
every value is one of those; or when it's on the short list below of values
that are Lumiverb's own words. Anything else fails it. The second runs real
commands against the API with names Rich would read as markup.
"""

# ruff: noqa: F811 — pytest fixtures are named as parameters

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from tests.test_scans_api import env  # noqa: F401 — the shared fixture: an account on the real API

ROOT = Path(__file__).resolve().parents[1]
CHECKED = (ROOT / "src" / "client", ROOT / "src" / "processing")

# Lumiverb's own words that the reading above can't tell are: (the file, the function or name), and why.
OWN_WORDS: dict[tuple[str, str], str] = {
}

# What escapes a value for Rich: rich.markup.escape, and scan.py's path printer (escaped, control characters shown).
_ESCAPERS = {"escape", "_shown"}
# Calls whose result is a number.
_NUMBERS = {"len", "int", "float", "round", "sum", "abs", "bool"}
# A format spec that makes a number of the value: `,`, `_`, or a number's presentation type.
_NUMBER_TYPES = set("bcdeEfFgGnoxX%")

# What Rich renders as markup: the method, and which of its arguments.
_PRINTS = {"print", "log", "rule", "status", "input"}
_RECEIVER = "console"  # every console in src/client and src/processing is named so (console, err_console, …)


def _number_format(spec: ast.JoinedStr | None) -> bool:
    if spec is None or not all(isinstance(v, ast.Constant) for v in spec.values):
        return False
    text = "".join(str(v.value) for v in spec.values)
    return bool(text) and (text[-1] in _NUMBER_TYPES or text.endswith((",", "_")))


def _name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _renderable(node: ast.expr) -> bool:
    """A table Lumiverb builds (its cells are checked where they're added)."""
    name = _name(node.func) if isinstance(node, ast.Call) else _name(node)
    return name is not None and (name == "table" or name.endswith("_table"))


class _Scope:
    """What a function (or the module) assigns to each name, and the names it can't know (parameters, loop
    variables, imports, …)."""

    def __init__(self, node: ast.AST, parent: _Scope | None, given: frozenset[str] = frozenset()) -> None:
        self.parent = parent
        self.values: dict[str, list[ast.expr]] = {}
        self.unknown: set[str] = set()
        self.imported: dict[str, tuple[str, str]] = {}  # from src.… import name: its module and name there
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a = node.args
            params = {x.arg for x in [*a.posonlyargs, *a.args, *a.kwonlyargs]}
            self.unknown |= params - given
            self.values |= {name: [ast.Constant("")] for name in params & given}  # a call's safe argument
            self.unknown |= {x.arg for x in (a.vararg, a.kwarg) if x is not None}
        for child in _own_nodes(node):
            if isinstance(child, ast.Assign):
                for target in child.targets:
                    self._assign(target, child.value)
            elif (isinstance(child, (ast.AnnAssign, ast.AugAssign)) and child.value is not None
                  or isinstance(child, ast.NamedExpr)):
                self._assign(child.target, child.value)
            elif (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                  and isinstance(child.func.value, ast.Name) and child.func.attr in ("append", "extend", "insert")
                  and child.args):
                # A list grown in place: what's added is among its values.
                added = child.args[-1]
                self._assign(child.func.value, ast.List([added]) if child.func.attr != "extend" else added)
            elif isinstance(child, (ast.For, ast.AsyncFor, ast.comprehension)):
                self._unknown(child.target)
            elif isinstance(child, ast.withitem) and child.optional_vars is not None:
                self._unknown(child.optional_vars)
            elif isinstance(child, ast.ExceptHandler) and child.name:
                self.unknown.add(child.name)
            elif isinstance(child, ast.ImportFrom) and child.level == 0 and (child.module or "").startswith("src."):
                self.imported |= {a.asname or a.name: (child.module, a.name) for a in child.names}
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                self.unknown |= {(a.asname or a.name).split(".")[0] for a in child.names}

    def _assign(self, target: ast.expr, value: ast.expr) -> None:
        if isinstance(target, ast.Name):
            self.values.setdefault(target.id, []).append(value)
        else:
            self._unknown(target)

    def _unknown(self, target: ast.expr) -> None:
        for node in ast.walk(target):
            if isinstance(node, ast.Name):
                self.unknown.add(node.id)

    def lookup(self, name: str) -> tuple[list[ast.expr], _Scope] | tuple[str, str] | None:
        """What's assigned to the name where it's read, and in which scope; the module and name it's imported
        from (src's own); None when that can't be known."""
        if name in self.unknown:
            return None
        if name in self.values:
            return self.values[name], self
        if name in self.imported:
            return self.imported[name]
        return self.parent.lookup(name) if self.parent is not None else None


def _own_nodes(node: ast.AST):
    """The nodes of a function's (or the module's) own body: not those of the functions and classes in it."""
    todo = list(ast.iter_child_nodes(node))
    while todo:
        child = todo.pop()
        yield child
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            todo.extend(ast.iter_child_nodes(child))


class _Checker:
    def __init__(self, path: Path, tree: ast.Module, files: dict[Path, _Checker]) -> None:
        self.rel = path.relative_to(ROOT).as_posix()
        self.tree = tree
        self.found: list[str] = []
        self.functions = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.module = _Scope(tree, None)
        self.scopes: dict[tuple[int, int, frozenset[str]], _Scope] = {}
        self.files = files
        files[path] = self
        self._deciding: set[int] = set()

    def scope(self, node: ast.AST, parent: _Scope, given: frozenset[str] = frozenset()) -> _Scope:
        """A function's (or comprehension's) scope; given: its parameters known to be safe (a call's)."""
        key = (id(node), id(parent), given)
        if key not in self.scopes:
            self.scopes[key] = _Scope(node, parent, given)
        return self.scopes[key]

    def given(self, func: ast.FunctionDef, call: ast.Call | None, scope: _Scope) -> frozenset[str]:
        """The parameters a call gives safe values (or leaves at a safe default)."""
        if call is None:
            return frozenset()
        a = func.args
        params = [*a.posonlyargs, *a.args]
        passed: dict[str, ast.expr] = {p.arg: v for p, v in zip(params, call.args, strict=False)}
        passed |= {k.arg: k.value for k in call.keywords if k.arg is not None}
        defaults = dict(zip([p.arg for p in params][len(params) - len(a.defaults):], a.defaults, strict=True))
        defaults |= {p.arg: d for p, d in zip(a.kwonlyargs, a.kw_defaults, strict=True) if d is not None}
        safe = set()
        for name in [p.arg for p in [*params, *a.kwonlyargs]]:
            if name in passed:
                if self.safe(passed[name], scope):
                    safe.add(name)
            elif name in defaults and self.safe(defaults[name], self.module):
                safe.add(name)
        return frozenset(safe)

    def own(self, name: str | None) -> bool:
        return (self.rel, name) in OWN_WORDS

    def other(self, module: str) -> _Checker | None:
        """The checker of a module of src's own, by its dotted name."""
        base = ROOT.joinpath(*module.split("."))
        path = next((p for p in (base.with_suffix(".py"), base / "__init__.py") if p.is_file()), None)
        if path is None:
            return None
        if path not in self.files:
            _Checker(path, ast.parse(path.read_text(), str(path)), self.files)
        return self.files[path]

    def returns_safe(self, func: ast.FunctionDef, given: frozenset[str] = frozenset()) -> bool:
        """Whether every value the function returns is safe, its parameters in given safe."""
        if id(func) in self._deciding:
            return True  # it calls itself: its other returns decide
        self._deciding.add(id(func))
        try:
            scope = self.scope(func, self.module, given)
            returns = [n.value for n in _own_nodes(func) if isinstance(n, ast.Return)]
            return bool(returns) and all(r is None or self.safe(r, scope) for r in returns)
        finally:
            self._deciding.discard(id(func))

    def imported_safe(self, origin: tuple[str, str], *, called: ast.Call | None = None,
                      scope: _Scope | None = None) -> bool:
        """An imported name (or a call of it, from scope): what it is in its own module."""
        module, name = origin
        other = self.other(module)
        if other is None:
            return False
        if other.own(name):
            return True
        if called is not None:
            if name not in other.functions:
                return False
            function = other.functions[name]
            # The call's arguments are judged here, where they're written.
            return other.returns_safe(function, self.given(function, called, scope or self.module))
        found = other.module.lookup(name)
        if found is not None and isinstance(found[0], str):
            return other.imported_safe(found)
        return found is not None and all(other.safe(v, found[1]) for v in found[0])

    def safe(self, node: ast.expr, scope: _Scope) -> bool:
        """A value that prints as itself: escaped, a constant, a number, a table, or Lumiverb's own."""
        if id(node) in self._deciding:
            return True  # a name's value read from itself (x = x + …): its other values decide
        self._deciding.add(id(node))
        try:
            return self._safe(node, scope)
        finally:
            self._deciding.discard(id(node))

    def _safe(self, node: ast.expr, scope: _Scope) -> bool:
        if isinstance(node, ast.Constant) or _renderable(node):
            return True
        if isinstance(node, ast.JoinedStr):
            return not self.unsafe_parts(node, scope)
        if isinstance(node, ast.IfExp):
            return self.safe(node.body, scope) and self.safe(node.orelse, scope)
        if isinstance(node, ast.BoolOp):
            return all(self.safe(v, scope) for v in node.values)
        if isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.Add):
                return self.safe(node.left, scope) and self.safe(node.right, scope)
            if isinstance(node.op, ast.Mod) and isinstance(node.left, ast.Constant):
                return self.safe(node.right, scope)
            return True  # arithmetic: a number
        if isinstance(node, (ast.Tuple, ast.List)):
            return all(self.safe(e, scope) for e in node.elts)
        if isinstance(node, (ast.GeneratorExp, ast.ListComp)):
            return self.safe(node.elt, self.scope(node, scope))
        if isinstance(node, ast.Starred):
            return self.safe(node.value, scope)
        if isinstance(node, ast.Name):
            if self.own(node.id):
                return True
            found = scope.lookup(node.id)
            if found is not None and isinstance(found[0], str):
                return self.imported_safe(found)
            return found is not None and all(self.safe(v, found[1]) for v in found[0])
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            return self._constant_table(node.value, scope)  # WORDS[key]: a dict of Lumiverb's words
        if isinstance(node, ast.Call):
            return self._safe_call(node, scope)
        return False

    def _constant_table(self, name: ast.Name, scope: _Scope) -> bool:
        found = scope.lookup(name.id)
        if found is not None and isinstance(found[0], str):
            other = self.other(found[0])
            return other is not None and other._constant_table(ast.Name(found[1]), other.module)
        return found is not None and all(
            isinstance(v, ast.Dict) and all(self.safe(x, found[1]) for x in v.values) for v in found[0])

    def _safe_call(self, node: ast.Call, scope: _Scope) -> bool:
        func, name = node.func, _name(node.func)
        if name in _ESCAPERS or self.own(name):
            return True
        if isinstance(func, ast.Name) and name in _NUMBERS:
            return True
        if isinstance(func, ast.Name) and name == "str" and len(node.args) == 1:
            return self.safe(node.args[0], scope)
        if isinstance(func, ast.Name) and name == "format":
            return True  # format(n, ","): a number's
        if isinstance(func, ast.Attribute) and name == "join" and isinstance(func.value, ast.Constant):
            return all(self.safe(a, scope) for a in node.args)
        if isinstance(func, ast.Attribute) and name in ("lower", "upper", "title", "strip"):
            return self.safe(func.value, scope)
        if isinstance(func, ast.Attribute) and name == "strftime" and all(
                isinstance(a, ast.Constant) for a in node.args):
            return True  # a time, in Lumiverb's format
        if isinstance(func, ast.Attribute) and name == "get" and isinstance(func.value, ast.Name):
            return self._constant_table(func.value, scope) and all(self.safe(a, scope) for a in node.args[1:])
        if isinstance(func, ast.Attribute) and name == "format" and isinstance(func.value, ast.Constant):
            return not self.unsafe_parts(node, scope)
        if isinstance(func, ast.Name) and name in self.functions:
            function = self.functions[name]
            return self.returns_safe(function, self.given(function, node, scope))
        if isinstance(func, ast.Name):
            found = scope.lookup(func.id)
            return (found is not None and isinstance(found[0], str)
                    and self.imported_safe(found, called=node, scope=scope))
        return False

    def unsafe_parts(self, node: ast.expr, scope: _Scope) -> list[ast.expr]:
        """The parts of a printed argument that Rich may read as markup."""
        if isinstance(node, ast.JoinedStr):
            return [part.value for part in node.values if isinstance(part, ast.FormattedValue)
                    and not (_number_format(part.format_spec) or self.safe(part.value, scope))]
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format"
                and isinstance(node.func.value, ast.Constant)):
            return [a for a in [*node.args, *(k.value for k in node.keywords)] if not self.safe(a, scope)]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self.unsafe_parts(node.left, scope) + self.unsafe_parts(node.right, scope)
        if isinstance(node, ast.IfExp):
            return self.unsafe_parts(node.body, scope) + self.unsafe_parts(node.orelse, scope)
        return [] if self.safe(node, scope) else [node]

    def check(self, call: ast.Call, args: list[ast.expr], scope: _Scope) -> None:
        for arg in args:
            for bad in self.unsafe_parts(arg, scope):
                self.found.append(f"{self.rel}:{bad.lineno}: {ast.unparse(bad)}")

    def visit(self, node: ast.AST, scope: _Scope) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                self.visit(child, self.scope(child, scope))
                continue
            if isinstance(child, ast.Call):
                self.printed(child, scope)
            self.visit(child, scope)

    def printed(self, node: ast.Call, scope: _Scope) -> None:
        func, name = node.func, _name(node.func)
        keywords = {k.arg: k.value for k in node.keywords}
        if isinstance(keywords.get("markup"), ast.Constant) and keywords["markup"].value is False:
            return  # printed as text
        if isinstance(func, ast.Attribute) and (
                name in _PRINTS and _RECEIVER in ast.unparse(func.value).lower() or name == "add_row"):
            self.check(node, list(node.args), scope)
        elif name in ("Table", "Panel", "add_column") and "title" in keywords:
            self.check(node, [keywords["title"]], scope)
        elif isinstance(func, ast.Attribute) and name == "add_task":
            self.check(node, [*node.args[:1], *([keywords["description"]] if "description" in keywords else [])],
                       scope)
        elif isinstance(func, ast.Attribute) and name == "update" and "description" in keywords:
            self.check(node, [keywords["description"]], scope)
        elif isinstance(func, ast.Attribute) and name == "ask" and node.args:
            self.check(node, [node.args[0]], scope)  # Prompt.ask, Confirm.ask


def unescaped(paths: list[Path]) -> list[str]:
    found: list[str] = []
    files: dict[Path, _Checker] = {}
    for path in paths:
        checker = files.get(path) or _Checker(path, ast.parse(path.read_text(), str(path)), files)
        checker.visit(checker.tree, checker.module)
        found += checker.found
    return found


def _sources() -> list[Path]:
    return sorted(p for folder in CHECKED for p in folder.rglob("*.py"))


def test_every_value_printed_is_text_rich_reads_as_text() -> None:
    found = unescaped(_sources())
    assert not found, ("Escape these (rich.markup.escape) or format them as numbers; Lumiverb's own words go on "
                       "OWN_WORDS:\n" + "\n".join(found))


def test_the_own_words_are_still_there() -> None:
    # A name on the list that's gone would let a new one by that name through unread.
    for (rel, name), _why in OWN_WORDS.items():
        tree = ast.parse((ROOT / rel).read_text())
        assert any(isinstance(n, (ast.FunctionDef, ast.Name)) and getattr(n, "name", getattr(n, "id", None)) == name
                   for n in ast.walk(tree)), (rel, name)


def test_the_check_finds_what_it_is_for(tmp_path, monkeypatch) -> None:
    # The check itself, on code written as the CLI was before: it would pass anything if it read nothing.
    source = tmp_path / "src" / "client" / "x.py"
    source.parent.mkdir(parents=True)
    (tmp_path / "src" / "client" / "words.py").write_text(
        "def clips(n):\n"
        "    return f'{n:,} clips'\n"
        "def named(lib):\n"
        "    return lib['name']\n"
        "def plural(n, one, many):\n"
        "    return f'{n:,} {one if n == 1 else many}'\n"
    )
    source.write_text(
        "from rich.markup import escape\n"
        "from src.client.words import clips, named, plural\n"
        "WORDS = {'on': 'On', 'off': 'Off'}\n"
        "def counted(n):\n"
        "    return f'{n:,} clips'\n"
        "def f(console, table, lib, n, progress, rows):\n"
        "    verb = 'Removed' if n else 'Kept'\n"
        "    said = f'[red]{escape(lib[\"why\"])}[/red]'\n"
        "    console.print(f\"Library {lib['name']} has {n} clips\")\n"
        "    console.print(f\"{verb} {escape(lib['name'])}: {n:,} clips, {len(lib):d}, {counted(n)}, {said}\")\n"
        "    console.print('Removed {}'.format(lib['name']))\n"
        "    table.add_row(lib['name'], str(n), escape(lib['id']), WORDS.get(lib['state'], 'Off'))\n"
        "    progress.add_task(f\"Scanning {lib['root']}\")\n"
        "    console.print(lib['message'], markup=False)\n"
        "    console.print(table)\n"
        "    for row in rows:\n"
        "        console.print(row)\n"
        "    console.print(f\"{clips(n)}, {plural(n, 'photo', 'photos')}, {plural(n, lib['one'], 'photos')}, {named(lib)}\")\n"
        "    cells = [escape(lib['id'])]\n"
        "    cells.append(lib['root'])\n"
        "    table.add_row(*cells)\n"
    )
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    found = [line.split(": ", 1)[1] for line in unescaped([source])]
    assert found == ["lib['name']", "n", "lib['name']", "lib['name']", "str(n)", "lib['root']", "row",
                     "plural(n, lib['one'], 'photos')", "named(lib)", "*cells"]


# ---------------------------------------------------------------------------
# Real commands, against the API
# ---------------------------------------------------------------------------

# What Rich would read as markup: a closing tag that opens nothing (a MarkupError), a link, a style.
MARKUP_NAMES = ["x[/b]", "[link=https://example.com]y[/link]", "[bold red]z"]


class _Through:
    """The CLI's client's connection, sent through the test's API with a key."""

    def __init__(self, env, headers: dict) -> None:
        self._env, self._headers = env, headers

    def request(self, method: str, url: str, **kw):
        return self._env.client.request(method, url, headers={**self._headers, **(kw.pop("headers", None) or {})},
                                        **kw)

    def close(self) -> None:
        pass


def _cli(env, monkeypatch, *args: str):
    import importlib
    from unittest.mock import patch

    from typer.testing import CliRunner

    from src.client.cli.client import LumiverbClient

    main = importlib.import_module("src.client.cli.main")  # the module (the package names a function main)
    projects = importlib.import_module("src.client.cli.commands.projects")

    client = LumiverbClient(base_url="http://testserver", token="unused")
    client._client = _Through(env, env.admin)  # type: ignore[assignment]
    for console in (main.console, projects.console):
        monkeypatch.setattr(console, "_width", 400)  # a table's cells on one line each, to read back whole
    with patch("src.client.cli.main.LumiverbClient", return_value=client), \
            patch("src.client.cli.commands.projects.LumiverbClient", return_value=client):
        result = CliRunner().invoke(main.app, list(args))
    assert result.exit_code == 0, result.output + repr(result.exception)
    return result.output


@pytest.mark.slow
def test_names_rich_would_read_as_markup_are_printed_as_they_are(env, monkeypatch, tmp_path) -> None:
    import uuid

    monkeypatch.setenv("HOME", str(tmp_path))  # this machine's CLI config: none
    tag = uuid.uuid4().hex[:6]
    for i, name in enumerate(MARKUP_NAMES):
        made = _cli(env, monkeypatch, "library", "create", "--name", f"{name}{tag}", "--path", f"/Volumes/{tag}-{i}")
        assert f"  name: {name}{tag}\n" in made, made
        made = _cli(env, monkeypatch, "project", "create", "--name", f"{name}{tag}", "--description", name)
        assert f"  name: {name}{tag}\n" in made and f"  description: {name}\n" in made, made
    libraries = _cli(env, monkeypatch, "library", "list")
    projects = _cli(env, monkeypatch, "project", "list")
    for name in MARKUP_NAMES:
        assert f" {name}{tag} " in libraries, libraries
        assert f" {name}{tag} " in projects, projects
