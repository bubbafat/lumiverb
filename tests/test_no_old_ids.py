"""No old id where a name belongs (ADR-024 phase 1 part 3): the artifacts',
lineage's producers' and job kinds' old ids are written nowhere in src, the
web's and the apps' code or their tests but where a family that keeps its
word has them (tests/old_ids_allowed.py, each entry with its family and
file). tests/old_ids.py finds them.
"""

from __future__ import annotations

import pytest

from tests.old_ids import _plugin_work, _python, _script, matches
from tests.old_ids_allowed import ALLOWED

pytestmark = pytest.mark.fast


def _allowed() -> set[tuple[str, str]]:
    return {(path, word) for words in ALLOWED.values() for word, paths in words.items() for path in paths}


def test_no_old_id_but_where_its_family_keeps_it():
    allowed = _allowed()
    left = [f"{path}:{line}: {word}" for path, line, word in matches() if (path, word) not in allowed]
    assert left == [], ("an old id where a name belongs: rename it, or allow it in tests/old_ids_allowed.py under the"
                        " family that keeps it\n" + "\n".join(left))


def test_the_allow_list_has_nothing_left_over():
    seen = {(path, word) for path, _, word in matches()}
    stale = sorted(f"{family}: {word} in {path}" for family, words in ALLOWED.items() for word, paths in words.items()
                   for path in paths if (path, word) not in seen)
    assert stale == [], "allowed, and not there any more: take it out of tests/old_ids_allowed.py\n" + "\n".join(stale)


def test_each_way_an_old_id_is_written_is_found():
    work = _plugin_work(frozenset({"geonames", "my_facts"}))
    python = '''
a = "vision"
b = "SELECT 1 FROM artifact_lineage WHERE artifact = 'scene_vision' AND producer = 'scene-detect'"
c = "producer.transcript.version_seen"
d = "missing_ocr"
e = f"redo_{kind}" if x else "redo_render"
f = client.post("/v1/producers/faces/pause")
g = "plugin_geonames"
h = f"plugin_{plugin_id}"
i = "plugin_" + plugin_id
# "vision" in a comment, and the module src/producers/faces/detect.py, aren't
j = "src/producers/faces/detect.py"
k = "lumiverb.image.description"
'''
    found = sorted(set(_python(python, work)))
    assert found == [(2, "vision"), (3, "scene-detect"), (3, "scene_vision"), (4, "producer.transcript"),
                     (5, "missing_ocr"), (6, "redo_render"), (7, "/producers/faces"), (8, "plugin_geonames"),
                     (9, "plugin_{"), (10, "plugin_")], found
    script = '''
const a = "capture";
const b = `/v1/producers/transcript/settings`;
// const c = "vision";
const d = { kind: 'render', artifact: "lumiverb.video.analysis_proxy" };
let e = "plugin_my_facts";
'''
    assert sorted(set(_script(script, work))) == [(2, "capture"), (3, "/producers/transcript"), (5, "render"),
                                                   (6, "plugin_my_facts")]
