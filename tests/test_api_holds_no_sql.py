"""The API layer builds no SQL: a route (src/server/api/) asks a repository
(src/server/repository/) for what it needs, and the repository writes the
query. A route may still end its transaction (commit, rollback, close).

What counts as building SQL here: calling sqlalchemy's or sqlmodel's
statement builders (text, select, insert, update, delete, …) and running
anything on a session (exec, execute, query, scalar(s), get, add, delete,
merge, flush, refresh).
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
    building = [line for p in _files() for line in sql_in(p)]
    assert not building, "Routes ask a repository (src/server/repository/) instead of building SQL:\n" + "\n".join(building)


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
