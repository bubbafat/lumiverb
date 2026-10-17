"""The API layer builds no SQL: a route (src/server/api/) asks a repository
(src/server/repository/) for what it needs, and the repository writes the
query. A route may still end its transaction (commit, rollback, close).

What counts as building SQL here: calling sqlalchemy's or sqlmodel's
statement builders (text, select, insert, update, delete, …) and running
anything on a session (exec, execute, query, scalar(s), get, add, delete,
merge, flush, refresh).

The files listed in NOT_YET still do, until their SQL moves (one area at a
time): the list only shrinks.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.fast

API = Path(__file__).resolve().parent.parent / "src" / "server" / "api"
BUILDERS = {"text", "select", "insert", "update", "delete", "func", "and_", "or_", "column", "literal_column",
            "bindparam", "union", "union_all", "exists", "tuple_", "case", "cast"}
SESSION_CALLS = {"exec", "execute", "query", "scalar", "scalars", "get", "add", "add_all", "delete", "merge",
                 "flush", "refresh", "get_bind", "connection"}

NOT_YET = {
    "routers/admin.py",
    "routers/keys.py",
    "routers/libraries.py",
    "routers/producers.py",
    "routers/projects.py",
    "routers/scans.py",
    "routers/system.py",
    "routers/tenant.py",
    "routers/upkeep.py",
}


def _session_like(node: ast.expr) -> bool:
    """A name or attribute that holds a database session (session, db, s, …_session)."""
    name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ""
    return name in {"session", "db", "s", "sess", "conn", "connection"} or name.endswith(("_session", "session_"))


def sql_in(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    builders = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in ("sqlalchemy", "sqlmodel"):
            builders |= {alias.asname or alias.name for alias in node.names if alias.name in BUILDERS}
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in builders:
            found.append((node.lineno, f"{func.id}(…)"))
        elif isinstance(func, ast.Attribute) and func.attr in SESSION_CALLS and _session_like(func.value):
            found.append((node.lineno, f".{func.attr}(…)"))
    return [f"{path.name}:{line} {what}" for line, what in sorted(found)]


def _files() -> list[Path]:
    return sorted(p for p in API.rglob("*.py") if "__pycache__" not in p.parts)


def test_the_api_builds_no_sql():
    found = {str(p.relative_to(API)): sql_in(p) for p in _files()}
    building = {name: lines for name, lines in found.items() if lines and name not in NOT_YET}
    assert not building, (
        "Routes ask a repository (src/server/repository/) instead of building SQL:\n"
        + "\n".join(line for lines in building.values() for line in lines))


def test_the_list_only_shrinks():
    """A file that no longer builds SQL comes off NOT_YET."""
    clean = [name for name in NOT_YET if (API / name).exists() and not sql_in(API / name)]
    missing = [name for name in NOT_YET if not (API / name).exists()]
    assert not clean and not missing, f"take these off NOT_YET: {clean + missing}"


def test_it_finds_what_it_says_it_finds(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from sqlalchemy import text as t\n"
        "from sqlmodel import select\n"
        "def route(session, repo):\n"
        "    session.execute(t('SELECT 1'))\n"
        "    session.exec(select(X)).all()\n"
        "    self._session.get(X, 1)\n"
        "    repo.get(1)\n"  # a repository's own method: fine
        "    {}.get('k')\n"
        "    session.commit()\n")
    assert sql_in(sample) == ["sample.py:4 .execute(…)", "sample.py:4 t(…)", "sample.py:5 .exec(…)",
                              "sample.py:5 select(…)", "sample.py:6 .get(…)"], "not repo.get, a dict's get or commit"
