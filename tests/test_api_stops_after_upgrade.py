"""The API stops cleanly when its version's files are gone: brew upgrade's cleanup can remove the old version's
files before its API stops (Oct 30: "No module named 'src.server.faces'" as 0.1.158 stopped), so its stop imports
nothing it didn't import before serving."""

from __future__ import annotations

import importlib.abc
import sys

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.fast


class _Gone(importlib.abc.MetaPathFinder):
    """Lumiverb's files deleted: any of its modules not imported yet can't be."""

    def find_spec(self, fullname, path, target=None):
        if fullname == "src" or fullname.startswith("src."):
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return None


def test_the_api_stops_with_its_files_gone(monkeypatch):
    from src.server.api.main import app

    faces = sys.modules.pop("src.server.faces.server_face_provider", None)
    package = sys.modules.pop("src.server.faces", None)
    try:
        with TestClient(app):
            monkeypatch.setattr(sys, "meta_path", [_Gone(), *sys.meta_path])
        # Leaving the block ran the lifespan's stop: it raised nothing.
    finally:
        monkeypatch.undo()
        if package is not None:
            sys.modules["src.server.faces"] = package
        if faces is not None:
            sys.modules["src.server.faces.server_face_provider"] = faces
