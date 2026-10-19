"""The built-in Place names removed (ADR-023 phase 3, part 4): GeoNames is a
plug-in like any other, so nothing in Lumiverb's code names it but its own
program (src/own_plugins/geonames/); the old producer's routes still answer
410 gone, naming what replaced them in no plug-in's name; and with no
plug-in that finds places, Place search says why (GET /v1/places' 409
plugin_off), naming none."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from tests.test_first_run_setup import control  # noqa: F401 (a fixture)
from tests.test_server_admins import install  # noqa: F401 (a fixture)

SRC = Path(__file__).resolve().parents[1] / "src"
OWN = SRC / "own_plugins" / "geonames"
# What Lumiverb runs and ships: its code, its pages', its plug-ins' (a page's tests, beside it, aren't).
SHIPPED = (".py", ".ts", ".tsx", ".js", ".json", ".html", ".css", ".sh")
SKIPPED = {"node_modules", "dist", "__pycache__"}


@pytest.mark.fast
def test_nothing_in_lumiverbs_code_names_geonames_but_its_own_program() -> None:
    named = []
    for root, dirs, files in os.walk(SRC):
        dirs[:] = [d for d in dirs if d not in SKIPPED and not d.startswith(".") and Path(root, d) != OWN]
        for name in files:
            path = Path(root, name)
            if path.suffix not in SHIPPED or re.search(r"\.test\.tsx?$", name):
                continue
            for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                if "geonames" in line.lower():
                    named.append(f"{path.relative_to(SRC.parent)}:{n}: {line.strip()[:100]}")
    assert named == [], "\n".join(named)
    assert (OWN / "lumiverb-plugin.json").is_file(), "it looked where GeoNames is"


@pytest.mark.slow
@pytest.mark.parametrize("path", ["/v1/places/nearest", "/v1/places/names"])
def test_the_old_producers_routes_stay_gone(install, path) -> None:  # noqa: F811
    client, robert, _ = install
    r = client.post(path, json={}, headers=robert)
    assert r.status_code == 410, r.text
    err = r.json()["error"]
    assert err["code"] == "gone" and "an installed plug-in's work names clips" in err["message"], err
    assert err["details"]["replaced_by"] == [
        f"POST /v1/plugins/{{plugin_id}}/{'inputs' if path.endswith('nearest') else 'outputs'}"]


@pytest.mark.slow
def test_with_no_plug_in_that_finds_places_place_search_says_why(install) -> None:  # noqa: F811
    """None installed (GeoNames uninstalled by the server admin, or its install failed): a 409 saying so, naming
    none, never a search answered from anywhere else."""
    client, robert, _ = install
    r = client.get("/v1/places", params={"q": "paris"}, headers=robert)
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["code"] == "plugin_off" and err["details"] == {"plugins": []}
    assert err["message"] == ("No plug-in that finds places is installed on this server: the server admin installs "
                              "one.")
    r = client.put("/v1/assets/locations", json={"asset_ids": ["ast_nope"], "place_id": "geonames:2988507"},
                   headers=robert)
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_place", r.text
