"""A plug-in's manifest (ADR-023): lumiverb-plugin.json, read and checked
before any of its code runs. The ADR's own example reads as written; every
kind of mistake is refused with its field named; a setting checks values
as Settings › Plug-ins will save them; what it reads and reaches is said in
words, and an update asking more is told apart from one asking the same."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.plugins.manifest import MANIFEST, ManifestError, asks_more, parse, read

pytestmark = pytest.mark.fast

EXAMPLE = {
    "id": "geonames",
    "version": "4",
    "protocol": 1,
    "title": "Place names (GeoNames)",
    "description": "Find a place by name when setting a location, and see the nearest town and the landmark.",
    "author": "Lumiverb",
    "homepage": "https://www.geonames.org/",
    "run": ["bin/geonames-plugin"],
    "acknowledgment": {"version": "3", "reaccept_from": "1", "title": "GeoNames: Creative Commons Attribution 4.0",
                       "text": "Place names come from GeoNames.",
                       "links": [{"label": "The license (CC BY 4.0)",
                                  "url": "https://creativecommons.org/licenses/by/4.0/"}]},
    "credit": {"text": "GeoNames", "url": "https://www.geonames.org/"},
    "downloads": [
        {"url": "https://download.geonames.org/export/dump/cities1000.zip", "about_mb": 11,
         "title": "Towns of 1,000 people or more"},
        {"url": "https://download.geonames.org/export/dump/allCountries.zip", "about_mb": 403,
         "title": "Every place, for its landmarks"},
    ],
    "data_version": 3,
    "internet": False,
    "memory_mb": 1024,
    "disk_mb": 2000,
    "settings": [
        {"key": "radius_km", "type": "int", "default": 25, "minimum": 1, "maximum": 200, "unit": "km",
         "label": "Nearest town within", "help": "How far away a town can be and still name where a clip was.",
         "remakes": True},
        {"key": "mirror", "type": "text", "scope": "server", "default": "", "pattern": "^(https://.+)?$",
         "max_length": 500, "label": "Download from", "help": "A mirror of download.geonames.org.", "advanced": True},
    ],
    "work": {"title": "Place names", "version": "2", "media": ["image", "video"], "inputs": ["location"],
             "batch": 200, "outputs": [{"key": "at", "kind": "fact", "label": "At"},
                                       {"key": "near", "kind": "fact", "label": "Near", "searchable": True}]},
    "lookups": ["places"],
}


def _problems(change) -> list[str]:
    data = copy.deepcopy(EXAMPLE)
    change(data)
    with pytest.raises(ManifestError) as raised:
        parse(data)
    return raised.value.problems


def test_the_adrs_example_reads_as_written() -> None:
    m = parse(copy.deepcopy(EXAMPLE))
    assert (m.plugin_id, m.version, m.protocol, m.run) == ("geonames", "4", 1, ("bin/geonames-plugin",))
    assert m.acknowledgment.version == "3" and m.acknowledgment.reaccept_from == "1"
    assert m.credit.url == "https://www.geonames.org/" and len(m.downloads) == 2
    assert (m.memory_mb, m.disk_mb, m.internet) == (1024, 2000, False)
    radius = m.setting("radius_km")
    assert (radius.type, radius.default, radius.minimum, radius.maximum, radius.remakes) == ("int", 25, 1, 200, True)
    assert m.setting("mirror").scope == "server" and m.setting("mirror").advanced
    assert m.work.inputs == ("location",) and [o.key for o in m.work.outputs] == ["at", "near"]
    assert m.lookups == ("places",)
    # As an account's switch sees it.
    spec = m.spec()
    assert (spec.plugin_id, spec.title, spec.acknowledgment) == ("geonames", m.title, m.acknowledgment)


def test_an_outputs_empty_line_and_a_credits_license() -> None:
    data = copy.deepcopy(EXAMPLE)
    data["work"]["outputs"][1]["empty"] = "  Not near  a town "
    data["credit"]["license"] = {"text": "CC BY 4.0", "url": "https://creativecommons.org/licenses/by/4.0/"}
    m = parse(data)
    assert [(o.key, o.empty) for o in m.work.outputs] == [("at", ""), ("near", "Not near a town")]
    assert m.credit.shown() == {"text": "GeoNames", "url": "https://www.geonames.org/", "license": {
        "text": "CC BY 4.0", "url": "https://creativecommons.org/licenses/by/4.0/"}}
    assert parse(copy.deepcopy(EXAMPLE)).credit.shown()["license"] is None, "naming none is fine"


def test_an_output_renamed_says_the_key_it_was_saved_under() -> None:
    # ADR-024 phase 2: a newer version that renames an output says what it was, so what's saved is renamed.
    data = copy.deepcopy(EXAMPLE)
    data["work"]["outputs"][0].update(key="landmark", was="at")
    data["work"]["outputs"][1].update(key="town", was="near")
    m = parse(data)
    assert [(o.key, o.was) for o in m.work.outputs] == [("landmark", "at"), ("town", "near")]
    assert [o.was for o in parse(copy.deepcopy(EXAMPLE)).work.outputs] == ["", ""], "none renamed"
    for was, change in (
        ("At", lambda o: o[0].update(was="At")),  # not a key
        ("its own", lambda o: o[0].update(was="at")),
        ("another's key", lambda o: o[0].update(key="landmark", was="near")),
        ("two outputs'", lambda o: (o[0].update(key="landmark", was="old"), o[1].update(key="town", was="old"))),
    ):
        problems = _problems(lambda d, change=change: change(d["work"]["outputs"]))
        assert any("was" in p for p in problems), (was, problems)


def test_only_what_it_needs_is_required() -> None:
    m = parse({"id": "hello", "version": "1", "protocol": 1, "title": "Hello", "description": "Says hello.",
               "run": ["python3", "hello.py"]})
    assert (m.settings, m.work, m.lookups, m.downloads, m.acknowledgment) == ((), None, (), (), None)
    assert (m.memory_mb, m.disk_mb, m.internet) == (1024, 1000, False)


@pytest.mark.parametrize(("change", "names"), [
    (lambda d: d.update(colour="red"), "colour: not a field Lumiverb knows"),
    (lambda d: d["work"].update(speed=3), "work.speed: not a field Lumiverb knows"),
    (lambda d: d.update(id="GeoNames"), "id: lowercase letters"),
    # A name ends where it ends: a final newline (which `$` lets by) reaches folders, sockets and SQL text.
    (lambda d: d.update(id="geonames\n"), "id: lowercase letters"),
    (lambda d: d["settings"][0].update(key="radius_km\n"), "settings[0].key: lowercase letters"),
    (lambda d: d["work"]["outputs"][0].update(key="town\n"), "work.outputs[0].key: lowercase letters"),
    (lambda d: d["work"]["outputs"][0].update(was="near\n"), "work.outputs[0].was: the key it had before"),
    (lambda d: d.update(version="3\n"), "version: numbers with dots"),
    (lambda d: d.pop("title"), "title: required"),
    (lambda d: d.update(version="four"), "version: numbers with dots"),
    (lambda d: d.update(protocol=2), "protocol: the plug-in protocol it speaks: 1"),
    (lambda d: d.update(protocol=True), "protocol:"),
    (lambda d: d.update(run=[]), "run: its command"),
    (lambda d: d.update(run=["/usr/bin/python3"]), "run[0]: a path in its folder, or a program's name"),
    (lambda d: d.update(run=["../elsewhere/run"]), "run[0]: a path in its folder"),
    (lambda d: d.update(homepage="http://example.com"), "homepage: an https:// address"),
    (lambda d: d["downloads"][0].update(url="ftp://example.com/x"), "downloads[0].url: an https:// address"),
    (lambda d: d["acknowledgment"].update(reaccept_from="4"), "acknowledgment.reaccept_from: a version up to its own"),
    (lambda d: d["acknowledgment"].update(text=" "), "acknowledgment.text: can't be empty"),
    (lambda d: d.update(memory_mb=100_000), "memory_mb: a whole number from 64 to 16,384"),
    (lambda d: d.update(internet="no"), "internet: true or false"),
    (lambda d: d["settings"][0].update(type="slider"), "settings[0].type: one of int, float, bool, text, choice"),
    (lambda d: d["settings"][0].update(default=500), "settings[0].default: Nearest town within is from 1 to 200 km"),
    (lambda d: d["settings"][0].pop("help"), "settings[0].help: required"),
    (lambda d: d["settings"][0].update(minimum=300), "settings[0].minimum: at most its maximum"),
    (lambda d: d["settings"][0].update(pattern="x"), "settings[0].pattern: only for text"),
    (lambda d: d["settings"][1].update(pattern="(unclosed"), "settings[1].pattern: a regular expression"),
    (lambda d: d["settings"][1].update(default="http://plain"), "settings[1].default: Download from isn't in the form"),
    (lambda d: d["settings"][1].update(key="radius_km"), "settings: radius_km twice"),
    (lambda d: d["settings"].append({"key": "token", "type": "secret", "default": "abc", "label": "Token",
                                     "help": "Its token."}), "settings[2].default: empty"),
    (lambda d: d["settings"].append({"key": "unit", "type": "choice", "default": "mi", "label": "Unit", "help": "x",
                                     "choices": [{"value": "km", "label": "Kilometres"}]}),
     "settings[2].default: Unit is one of Kilometres"),
    (lambda d: d["settings"].append({"key": "unit", "type": "choice", "default": "km", "label": "Unit", "help": "x"}),
     "settings[2].choices: at least one"),
    (lambda d: d["work"].update(inputs=["faces"]), "work.inputs[0]: one of location, capture_time, metadata, preview"),
    (lambda d: d["work"].update(inputs=["preview"]), "work.inputs: preview is a photo's"),
    (lambda d: d["work"]["outputs"][0].update(kind="tags"), "work.outputs[0].kind: one of fact (protocol 1)"),
    (lambda d: d["work"].update(outputs=[]), "work.outputs: at least one"),
    (lambda d: d["work"].update(version=""), "work.version: can't be empty"),
    (lambda d: d.update(lookups=["weather"]), "lookups[0]: one of places"),
    (lambda d: d.update(lookups=["places", "places"]), "lookups[1]: places twice"),
    (lambda d: d["work"]["outputs"][1].update(empty="x" * 81), "work.outputs[1].empty: text, up to 80"),
    (lambda d: d["work"]["outputs"][1].update(empty="<b>none</b>"), "work.outputs[1].empty: plain text (no HTML)"),
    (lambda d: d["work"]["outputs"][1].update(empty="a\x07bell"), "work.outputs[1].empty: plain text"),
    (lambda d: d["credit"].update(license={"text": "CC BY 4.0"}), "credit.license.url: required"),
    (lambda d: d["credit"].update(license={"text": "CC BY", "url": "http://x.org"}), "credit.license.url: an https://"),
    (lambda d: d["credit"].update(license={"url": "https://x.org", "version": 4}), "credit.license.version: not a field"),
])
def test_each_mistake_is_refused_naming_its_field(change, names: str) -> None:
    problems = _problems(change)
    assert any(names in p for p in problems), problems


def test_every_problem_is_said_at_once() -> None:
    problems = _problems(lambda d: d.update(id="X", version="v", protocol=9))
    assert len(problems) == 3


def test_a_manifest_in_its_folder(tmp_path: Path) -> None:
    folder = tmp_path / "hello"
    (folder / "bin").mkdir(parents=True)
    data = {"id": "hello", "version": "1", "protocol": 1, "title": "Hello", "description": "Says hello.",
            "run": ["bin/hello"]}
    (folder / MANIFEST).write_text(json.dumps(data))
    with pytest.raises(ManifestError, match=r"run\[0\]: bin/hello isn't in its folder"):
        read(folder)
    (folder / "bin" / "hello").write_text("#!/bin/sh\n")
    assert read(folder).run == ("bin/hello",)
    for raw, why in ((b"{not json", "not JSON"), (b" " * (300 * 1024), "over 256 KB")):
        (folder / MANIFEST).write_bytes(raw)
        with pytest.raises(ManifestError, match=why):
            read(folder)
    with pytest.raises(ManifestError, match="can't be read"):
        read(tmp_path / "nowhere")


def test_a_setting_checks_values_as_they_will_be_saved() -> None:
    m = parse(copy.deepcopy(EXAMPLE))
    radius, mirror = m.setting("radius_km"), m.setting("mirror")
    assert radius.check(30) == 30 and radius.check(30.0) == 30
    for wrong, why in ((0, "from 1 to 200 km"), (2.5, "whole number"), (True, "a number"), ("30", "a number"),
                       (float("nan"), "from 1 to 200 km")):
        with pytest.raises(ValueError, match=why):
            radius.check(wrong)
    assert mirror.check("") == "" and mirror.check("https://mirror.example") == "https://mirror.example"
    with pytest.raises(ValueError, match="isn't in the form it takes"):
        mirror.check("ftp://mirror.example")
    with pytest.raises(ValueError, match="up to 500 characters"):
        mirror.check("https://" + "x" * 500)


def test_what_it_reads_and_reaches_in_words_and_an_update_that_asks_more() -> None:
    m = parse(copy.deepcopy(EXAMPLE))
    words = m.described()
    assert "Reads each clip's location (a person's, else its GPS, else an applied guess)" in words
    assert "Downloads Every place, for its landmarks (about 403 MB) from download.geonames.org" in words
    assert "Doesn't use the internet" in words and "Answers place searches (Set location's Place)" in words
    assert "Shows with each clip: At, Near (searchable)" in words
    before = m.permissions()
    assert before == {"inputs": ["location"], "download_hosts": ["download.geonames.org"], "internet": False,
                      "lookups": ["places"]}
    assert asks_more(before, before) == []
    more = copy.deepcopy(EXAMPLE)
    more["work"]["inputs"].append("capture_time")
    more["internet"] = True
    more["downloads"].append({"url": "https://example.org/x.zip", "title": "More", "about_mb": 1})
    assert asks_more(before, parse(more).permissions()) == [
        "Reads when each clip was taken", "Downloads from example.org", "Uses the internet"]
    less = copy.deepcopy(EXAMPLE)
    less.pop("lookups")
    assert asks_more(before, parse(less).permissions()) == []
