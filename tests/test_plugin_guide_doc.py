"""docs/writing-a-plugin.md is protocol 1's whole contract (ADR-023): what a
plug-in's author reads instead of Lumiverb's code. So what it states as
fields, lists, limits and times is held here to the code that decides them:
a change to one fails until the other says the same. Its example manifest
is one Lumiverb takes.

Read as text: the page's own words, as an author reads them."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.fast

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "writing-a-plugin.md"


def _page() -> str:
    return DOC.read_text(encoding="utf-8")


def _section(title: str) -> str:
    """From the heading to the next of its level or higher."""
    text = _page()
    start = text.index(f"\n{title}\n")
    level = len(title) - len(title.lstrip("#"))
    after = re.search(r"\n#{1,%d} " % level, text[start + 1:])
    return text[start:start + 1 + after.start() if after else len(text)]


def _first_column(section: str) -> set[str]:
    """The backticked names opening each row of the section's tables."""
    return {name for row in re.findall(r"^\| ([^|]+) \|", section, re.M) for name in re.findall(r"`([^`]+)`", row)}


def test_its_example_manifest_is_one_lumiverb_takes():
    from src.plugins.manifest import parse

    [example] = re.findall(r"```json\n(.*?)```", _section("## The manifest"), re.S)
    manifest = parse(json.loads(example))
    assert manifest.work is not None and manifest.settings


def test_the_fields_it_lists_are_the_ones_a_manifest_may_have():
    from src.plugins import manifest as m

    assert _first_column(_section("### Its fields")) == set(m.FIELDS)
    settings = _section("### Settings")
    assert all(f"`{field}`" in settings for field in m.SETTING_FIELDS)
    work = _section("### Its work")
    assert re.search(r"`\{" + ", ".join(f'"{f}"' for f in m.WORK_FIELDS) + r"\}`", work)
    assert all(f"`{field}`" in work for field in m.OUTPUT_FIELDS)


def test_the_kinds_it_lists_are_the_ones_lumiverb_knows():
    from src.plugins import manifest as m

    assert _first_column(_section("### Settings")) == set(m.SETTING_TYPES)
    assert _first_column(_section("### Its work")) == set(m.INPUTS)
    assert m.OUTPUT_KINDS == ("fact",) and "`fact` (protocol 1's only kind)" in _page()
    assert m.LOOKUPS.keys() == {"places"} and "`places` (Set location's Place search)" in _page()
    assert m.MEDIA == ("image", "video") and "`image`, `video` or both" in _page()
    assert m.SCOPES == ("account", "server") and "`scope`: `account` (the default" in _page()
    assert m.PROTOCOLS == (1,) and "The protocol it speaks: `1`" in _page()


def test_the_limits_it_states_are_the_manifests():
    from src.plugins import manifest as m
    from src.server import plugin_install

    said = _page()
    assert f"default {m.MEMORY_MB:,}, at most {m.MEMORY_MB_MAX:,}" in said
    assert f"default {m.DISK_MB:,}, at most {m.DISK_MB_MAX:,}" in said
    assert f"at most {m.TEXT_MAX:,})" in said
    assert f"1 to {m.BATCH_MAX} (default {m.BATCH})" in said
    assert f"plain text up to {m.EMPTY_MAX} characters" in said
    assert f"1 to {m.FACT_MAX} characters" in said
    assert f"up to {m.URL_MAX:,} characters" in said
    assert f"at most {m._TOO_BIG // 1024} KB of JSON" in said
    assert f"at most {plugin_install.MAX_BYTES // 1024**3} GiB and {plugin_install.MAX_FILES:,} files" in said


def test_what_it_says_of_the_runner_is_the_runners():
    from src.server import plugin_launch, plugin_runner as r

    said = _section("## How it's run")
    assert f"within {r.START_WITHIN_SEC:.0f} seconds of starting" in said
    assert f"every {r.HEALTH_EVERY_SEC:.0f} seconds, each answer within {r.HEALTH_TIMEOUT_SEC:.0f} seconds" in said
    assert f"missed {r.UNANSWERED} times in a row" in said
    assert f"then {r.STOP_WITHIN_SEC:.0f} seconds before" in said
    assert f"{r.RESTART_FIRST_SEC:.0f} seconds later at first" in said
    assert f"(within {r.STAYED_UP_SEC / 60:.0f} minutes), up to {r.RESTART_MAX_SEC / 60:.0f} minutes" in said
    assert f"after {r.GIVE_UP_AFTER} such stops" in said
    assert f"over {r.LINE_MAX:,} characters are cut, and past {r.LINES_PER_MINUTE} lines a minute" in said
    assert "nice 10" in said and "os.nice(10)" in Path(plugin_launch.__file__).read_text()
    assert f"every {r.HEALTH_STARTING_EVERY_SEC} seconds while starting" in _section("### GET /v1/health")


def test_the_environment_it_lists_is_what_a_plug_in_is_handed(tmp_path):
    from src.plugins.manifest import parse
    from src.server import plugin_runner as r
    from src.shared.utils import utcnow

    hello = parse(json.loads((REPO / "tests/plugins/hello/lumiverb-plugin.json").read_text()))
    runner = r.Runner.__new__(r.Runner)
    runner.data = tmp_path
    given = runner._environment(r.Plugin(plugin_id="hello", manifest=hello, program=("1", "", ""), since=utcnow()))
    own = {k for k in given if k not in r.HANDED_ON}
    listed = _first_column(_section("## How it's run"))
    assert {n for name in listed for n in name.split(", ")} | {"LUMIVERB_PLUGIN_PROTOCOL"} >= own
    assert own >= {n for name in listed for n in name.split(", ") if n.startswith(("LUMIVERB_", "HOME", "TMPDIR",
                                                                                     "PYTHONUNBUFFERED"))}
    handed = _section("## How it's run")
    assert all(f"`{name}`" in handed for name in r.HANDED_ON if name.upper() == name), "and their lowercase spellings"


def test_its_requests_are_the_ones_lumiverb_makes_with_their_times_and_sizes():
    from src.producers import plugin_work
    from src.server import plugin_client, plugin_lookups, plugin_runner

    said = _page()
    asked = {"/v1/health", "/v1/settings/check", "/v1/work", f"/v1/lookups/{plugin_lookups.PLACES}"}
    assert set(re.findall(r"^### (?:GET|POST) (\S+)$", said, re.M)) == asked
    for path, source in (("/v1/health", plugin_runner), ("/v1/work", plugin_work), ("/v1/settings/check", None)):
        if source is not None:
            assert path in Path(source.__file__).read_text(), path
    assert f"at most {plugin_client.ANSWER_MAX_BYTES // 1024} KB (a work request's, " \
           f"{plugin_work.ANSWER_MAX_BYTES // 2**20} MB)" in said
    assert f"It has {plugin_work.WORK_TIMEOUT_SEC:.0f} seconds for the whole batch" in said
    assert f"each with {plugin_lookups.LOOKUP_TIMEOUT_SEC:.0f} seconds" in said


def test_the_settings_check_has_the_time_the_api_gives_it():
    import inspect

    from src.server.api.routers import plugins
    from src.server.plugin_client import ask

    source = inspect.getsource(plugins)
    call = source[source.index('"/v1/settings/check"'):]
    call = call[:call.index(")") + 1]
    assert "timeout" not in call, "the default time limit, which the page says"
    assert inspect.signature(ask).parameters["timeout"].default == 5.0 and "It has 5 seconds" in _page()


def test_the_places_it_describes_are_what_lumiverb_takes():
    from src.server import plugin_lookups
    from src.server.api.routers import places

    said = _section("### POST /v1/lookups/places")
    assert "up to 100, each" in said and "len(said) > 100" in Path(plugin_lookups.__file__).read_text()
    assert "at most `limit` (1 to 25)" in said and "Query(ge=1, le=25)" in Path(places.__file__).read_text()
    for field in ("id", "name", "region", "country", "kind", "matched", "lat", "lon", "population"):
        assert f"`{field}`" in said, field


def test_the_examples_it_names_are_there():
    for folder in ("src/plugin_examples/example/", "tests/plugins/hello/"):
        assert f"`{folder}`" in _page() and (REPO / folder / "lumiverb-plugin.json").is_file()
