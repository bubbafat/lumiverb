"""Producers hold no files of their own (ADR-020 decision 4). What a clip is
made from comes from its job (Work.inputs: its stored preview and analysis
proxy, and its original only for a producer that reads the storage). Its
work files go in the job's scratch (Work.scratch), and what it makes is
saved through the API, an upload handed to the client as its path. So
src/producers, but its runner, imports nothing that finds, keeps or makes
files, and opens none itself.

Read as source: a call through an alias or a getattr would get past it, and
a reviewer would see it.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.producers import runner
from src.server.scheduler.dispatch import Job
from src.shared.producers import PRODUCERS

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parents[1]
PRODUCERS_DIR = ROOT / "src" / "producers"
RUNNER = PRODUCERS_DIR / "runner.py"

FORBIDDEN = {
    "a temporary file (the job's scratch is its)": r"\btempfile\b|TemporaryDirectory|NamedTemporaryFile"
                                                   r"|\bmkdtemp\b|\bmkstemp\b",
    "moving files about": r"\bshutil\b",
    "where things are kept": r"\bcache_dir\b|\bget_storage\b|\bget_store\b|storage\.(local|files|store)\b",
    "finding an original": r"\bresolve_source_path\b|\bcheck_rel_path\b|\bio_utils\b|\bacct\.root\(",
    "opening a path": r"(?<![\w.])open\(",
}
CHECK = re.compile("|".join(f"(?:{p})" for p in FORBIDDEN.values()))


def _sources() -> list[Path]:
    return [p for p in sorted(PRODUCERS_DIR.rglob("*.py")) if p != RUNNER and "__pycache__" not in p.parts]


def test_no_producer_finds_keeps_makes_or_opens_a_file_of_its_own():
    found = []
    for path in _sources():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if CHECK.search(code):
                why = next(w for w, p in FORBIDDEN.items() if re.search(p, code))
                found.append(f"{path.relative_to(ROOT)}:{n}: {why}: {line.strip()}")
    assert found == []


def test_the_check_would_find_one():
    for line in ("import tempfile", "with tempfile.TemporaryDirectory() as tmp:", "import shutil",
                 "from src.processing.cache_dir import cache_dir", "store = get_store()",
                 "from src.server.storage.local import get_storage", "source = resolve_source_path(root, rel)",
                 "root = self.acct.root(library_id)", "with open(path, 'rb') as f:"):
        assert CHECK.search(line), line
    for line in ("frame = self.scratch / 'frame-1.jpg'", "with self.inputs.original(clip) as source:",
                 "with Image.open(frame) as img:", "devnull = os.open(os.devnull, os.O_WRONLY)",
                 "files={'file': ('rep.jpg', s.frame, 'image/jpeg')}"):
        assert not CHECK.search(line), line


def test_only_a_producer_that_reads_the_storage_asks_for_originals():
    asking = {p.parent.name for p in _sources() if re.search(r"inputs\.originals?\(", p.read_text(encoding="utf-8"))}
    assert asking, "the check finds the ones that do"
    assert {a for a in asking if not PRODUCERS[a].storage} == set()


class _Asks(runner.Work):
    def make(self, clip: dict) -> None:
        with self.inputs.original(clip):
            pass

    def save(self, client, made) -> None:
        pass


def test_a_producer_that_doesnt_read_the_storage_is_refused_an_original():
    acct = MagicMock()
    acct.stopping = threading.Event()
    job = Job("t1", "clip", 3, ({"asset_id": "ast_1", "library_id": "lib_1", "rel_path": "a.jpg"},))
    work = type("ClipAsks", (_Asks,), {"artifact": "clip"})(acct, job)
    with pytest.raises(RuntimeError, match="doesn't read the originals"):
        work.make(job.items[0])
    acct.inputs.original.assert_not_called()
    stored = type("ProbeAsks", (_Asks,), {"artifact": "probe"})(acct, job)
    stored.make(job.items[0])
    acct.inputs.original.assert_called_once_with(job.items[0])


def test_an_upload_is_handed_to_the_client_as_its_path_and_opened_for_the_request(tmp_path):
    frame = tmp_path / "frame-1.jpg"
    frame.write_bytes(b"jpg")
    sent = {}

    def post(path, **kwargs):
        name, f, kind = kwargs["files"]["file"]
        sent.update(name=name, body=f.read(), kind=kind, open=not f.closed, other=kwargs["files"]["crops"])
        sent["f"] = f

    client = runner.Saving(MagicMock(post=post))
    client.post("/v1/x", files={"file": ("rep.jpg", frame, "image/jpeg"), "crops": ("c.webp", b"bytes", "image/webp")})
    assert (sent["name"], sent["body"], sent["kind"], sent["open"]) == ("rep.jpg", b"jpg", "image/jpeg", True)
    assert sent["other"] == ("c.webp", b"bytes", "image/webp"), "bytes go as they are"
    assert sent["f"].closed, "closed once the request is done"
    as_a_list = runner._opened([("crops", ("0.webp", frame, "image/webp"))], __import__("contextlib").ExitStack())
    assert as_a_list[0][1][1].read() == b"jpg"
