"""The service's log for Admin › Logs (GET /v1/system/logs): newest first, a
page at a time.

Where it is depends on how Lumiverb runs:
- A Mac (lumiverb-server run, launchd): one file, launchd's, rotated by run
  with LOG_KEPT older ones beside it (lifecycle/log.py). run tells the API
  where it is (LUMIVERB_LOG_FILE). Each line says whose it is: "api | ...",
  run's own "lumiverb-server: ...".
- Linux (systemd units): the journal, for Lumiverb's units (UNITS), read with
  journalctl, once whoever runs the server adds the service user to
  systemd-journal. Never done for them: that group reads the whole system
  journal (every service, sudo's command lines), not only Lumiverb's lines
  (Robert, Oct 11). Until then the page says how, and what it opens.
- Started from a terminal: the terminal has it; nothing here.

An entry is a line and the lines that continue it (a traceback): a line that
doesn't start like a record (a date, uvicorn's "INFO:") goes with the one
before it from the same part. A page reads at most SCAN_BYTES of the file
(SCAN_ENTRIES of the journal), so a search that finds little still answers
quickly: its next_cursor goes on from there.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from src.server.lifecycle.log import LOG_KEPT

Level = Literal["error", "warning", "info"]

UNITS = {
    "lumiverb-api.service": "api",
    "lumiverb-scheduler.service": "scheduler",
    "lumiverb-worker.service": "scheduler",  # what the scheduler replaced
    "lumiverb-quickwit.service": "quickwit",
    "lumiverb-upkeep.service": "upkeep",
    "lumiverb-upkeep-daily.service": "upkeep",
}
RUN = "lumiverb-server"  # run's own lines, on a Mac
SCAN_BYTES = 8 * 1024 * 1024
SCAN_ENTRIES = 20_000
COMPLETE_LINES = 500  # a full page reads on this far for the records its tracebacks belong to
JOURNAL_BATCH = 1000
JOURNAL_TIMEOUT_SEC = 10
BLOCK = 64 * 1024
JOURNAL_NOT_READABLE = (
    "Lumiverb can't read the journal. To show its log here: sudo usermod -aG systemd-journal lumiverb, "
    "then sudo systemctl restart lumiverb-api. That lets Lumiverb read the whole system journal "
    "(every service, sudo's command lines), not only its own lines.")

_NAMED = re.compile(r"^([a-z][a-z0-9-]*) \| (.*)$")
_RUNS_OWN = re.compile(rf"^{RUN}: (.*)$")
# A record starts with a date (Python's logging, PostgreSQL, Quickwit, nginx)
# or uvicorn's own "INFO:"; anything else continues the one before.
_STARTS = re.compile(r"^(\d{4}[-/]\d{2}[-/]\d{2}|(DEBUG|INFO|WARNING|ERROR|CRITICAL):)")
_ERROR = re.compile(r"\b(ERROR|CRITICAL|FATAL|PANIC|Traceback)\b|\[(error|crit|alert|emerg)\]")
_WARNING = re.compile(r"\b(WARNING|WARN)\b|\[warn\]")


class LogUnavailableError(Exception):
    """There's no log to show here, and why."""


@dataclass
class Entry:
    id: str
    part: str | None
    level: Level
    text: str
    at: str | None = None  # the journal's time; a file's lines carry their own


@dataclass
class Page:
    source: Literal["file", "journal"]
    where: str
    entries: list[Entry]
    next_cursor: str | None


def level_of(text: str) -> Level:
    if _ERROR.search(text):
        return "error"
    if _WARNING.search(text):
        return "warning"
    return "info"


def _starts_a_record(text: str) -> bool:
    return bool(_STARTS.match(text))


@dataclass
class _Line:
    key: tuple[int, int]  # sorts newest first: (older file → larger first part, -offset)
    id: str
    part: str | None
    text: str
    at: str | None = None


class _Grouper:
    """Lines newest first → (a record's first line, the lines continuing it in
    order), given at its first line. A full page reads on (COMPLETE_LINES at
    most) while a traceback waits for its record; any still waiting are given
    on their own (flush): never lost, as the next page starts below them."""

    def __init__(self) -> None:
        self.waiting: dict[str | None, list[_Line]] = {}

    def feed(self, line: _Line) -> tuple[_Line, list[_Line]] | None:
        if line.part is None or _starts_a_record(line.text):
            return line, list(reversed(self.waiting.pop(line.part, [])))
        self.waiting.setdefault(line.part, []).append(line)
        return None

    def flush(self) -> list[tuple[_Line, list[_Line]]]:
        left = [list(reversed(lines)) for lines in self.waiting.values()]
        self.waiting = {}
        return [(lines[0], lines[1:]) for lines in left]


def _matches(entry: Entry, part: str | None, problems: bool, text: str | None) -> bool:
    return ((part is None or entry.part == part) and (not problems or entry.level != "info")
            and (not text or text.casefold() in entry.text.casefold()))


def _entry(first: _Line, rest: list[_Line]) -> Entry:
    body = "\n".join([first.text, *(line.text for line in rest)])
    return Entry(id=first.id, part=first.part, level=level_of(body), text=body, at=first.at)


def _page(lines: Iterator[_Line], limit: int, part: str | None, problems: bool, text: str | None,
          more: Callable[[], str | None]) -> tuple[list[Entry], str | None]:
    found: list[tuple[tuple[int, int], Entry]] = []
    grouper = _Grouper()

    def take(first: _Line, rest: list[_Line]) -> None:
        entry = _entry(first, rest)
        if _matches(entry, part, problems, text):
            found.append((first.key, entry))

    full_at: int | None = None
    for n, line in enumerate(lines):
        record = grouper.feed(line)
        if record:
            take(*record)
        if full_at is None and len(found) >= limit:
            full_at = n
        if full_at is not None and (not grouper.waiting or n - full_at >= COMPLETE_LINES):
            break
    for record in grouper.flush():
        take(*record)
    found.sort(key=lambda kv: kv[0])
    return [entry for _, entry in found], more()


# ---------------------------------------------------------------------------
# A Mac's file
# ---------------------------------------------------------------------------


def _backward(f, end: int) -> Iterator[tuple[int, bytes]]:
    """(offset, line) of each line before `end`, the last first."""
    pos, carry = end, b""
    while True:
        start = max(0, pos - BLOCK)
        f.seek(start)
        data = f.read(pos - start) + carry
        pieces = data.split(b"\n")
        offsets, at = [], start
        for piece in pieces:
            offsets.append(at)
            at += len(piece) + 1
        for i in range(len(pieces) - 1, 0, -1):
            yield offsets[i], pieces[i]
        if start == 0:
            yield 0, pieces[0]
            return
        carry, pos = pieces[0], start


def _parsed(raw: bytes) -> tuple[str | None, str]:
    line = raw.decode("utf-8", "replace").rstrip("\r")
    named = _NAMED.match(line)
    if named:
        return named.group(1), named.group(2)
    own = _RUNS_OWN.match(line)
    return RUN, (own.group(1) if own else line)  # unnamed: run's own (a crash's traceback)


class _FileReader:
    def __init__(self, path: Path, after: str | None) -> None:
        files = [path, *(path.with_name(f"{path.name}.{n}") for n in range(1, LOG_KEPT + 1))]
        self.files: list[tuple[int, Path]] = []
        for candidate in files:
            try:
                self.files.append((candidate.stat().st_ino, candidate))
            except OSError:
                continue
        self.start_file, self.start_end = 0, None
        if after:
            inode, _, offset = after.partition(":")
            if not inode.isdigit() or not (offset.isdigit() or offset == "end"):
                raise ValueError(f"Not a cursor of this log: {after}")
            for index, (ino, _) in enumerate(self.files):
                if str(ino) == inode:
                    self.start_file, self.start_end = index, (None if offset == "end" else int(offset))
                    break
            else:
                self.files = []  # rotated away: nothing older is left
        self.read = 0
        self.oldest: tuple[int, int] | None = None  # (file index, offset) of the last line read
        self.done = False

    def lines(self) -> Iterator[_Line]:
        for index in range(self.start_file, len(self.files)):
            ino, path = self.files[index]
            try:
                f = path.open("rb")
            except OSError:
                continue
            with f:
                size = os.fstat(f.fileno()).st_size
                end = min(self.start_end, size) if index == self.start_file and self.start_end is not None \
                    else size
                for offset, raw in _backward(f, end):
                    self.oldest = (index, offset)
                    self.read += len(raw) + 1
                    if raw.strip():
                        part, text = _parsed(raw)
                        yield _Line(key=(index, -offset), id=f"{ino}:{offset}", part=part, text=text)
                    if self.read >= SCAN_BYTES:
                        return
        self.done = True

    def cursor(self) -> str | None:
        if self.done or self.oldest is None:
            return None
        index, offset = self.oldest
        if offset == 0:
            return f"{self.files[index + 1][0]}:end" if index + 1 < len(self.files) else None
        return f"{self.files[index][0]}:{offset}"


def file_page(path: Path, *, after: str | None = None, limit: int = 200, part: str | None = None,
              problems: bool = False, text: str | None = None) -> Page:
    if not path.exists():
        raise LogUnavailableError(f"The log ({path}) isn't there yet.")
    reader = _FileReader(path, after)
    entries, cursor = _page(reader.lines(), limit, part, problems, text, reader.cursor)
    return Page(source="file", where=str(path), entries=entries, next_cursor=cursor)


# ---------------------------------------------------------------------------
# Linux's journal
# ---------------------------------------------------------------------------


class _JournalReader:
    def __init__(self, after: str | None, run=None) -> None:
        self.after = after
        self.run = run or subprocess.run
        self.oldest: str | None = None
        self.count = 0
        self.done = False

    def _batch(self, after: str | None) -> list[dict]:
        argv = ["journalctl", "--no-pager", "-o", "json", "-r", "-n", str(JOURNAL_BATCH),
                *(f"--unit={unit}" for unit in UNITS)]
        if after:
            argv.append(f"--after-cursor={after}")  # with -r: older than it
        try:
            out = self.run(argv, capture_output=True, text=True, timeout=JOURNAL_TIMEOUT_SEC, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LogUnavailableError(f"journalctl didn't answer: {exc}") from exc
        records = []
        for raw in out.stdout.splitlines():
            try:
                records.append(json.loads(raw))
            except ValueError:
                continue
        if not records and after is None and "not seeing messages" in (out.stderr or ""):
            raise LogUnavailableError(JOURNAL_NOT_READABLE)
        if out.returncode != 0 and not records:
            raise LogUnavailableError(f"journalctl failed: {(out.stderr or '').strip()[:300]}")
        return records

    def lines(self) -> Iterator[_Line]:
        after = self.after
        while True:
            records = self._batch(after)
            for record in records:
                cursor = record.get("__CURSOR")
                self.oldest = cursor
                self.count += 1
                message = record.get("MESSAGE")
                if isinstance(message, list):  # bytes journald couldn't take as text
                    message = bytes(message).decode("utf-8", "replace")
                if message and str(message).strip():
                    yield _Line(key=(0, self.count), id=str(cursor), part=UNITS.get(record.get("_SYSTEMD_UNIT", "")),
                                text=str(message).rstrip(), at=_when(record.get("__REALTIME_TIMESTAMP")))
                if self.count >= SCAN_ENTRIES:
                    return
            if len(records) < JOURNAL_BATCH:
                self.done = True
                return
            after = self.oldest

    def cursor(self) -> str | None:
        return None if self.done else self.oldest


def _when(usec: object) -> str | None:
    try:
        return datetime.fromtimestamp(int(str(usec)) / 1_000_000, UTC).isoformat()
    except (TypeError, ValueError):
        return None


def journal_page(*, after: str | None = None, limit: int = 200, part: str | None = None, problems: bool = False,
                 text: str | None = None, run=None) -> Page:
    reader = _JournalReader(after, run)
    entries, cursor = _page(reader.lines(), limit, part, problems, text, reader.cursor)
    return Page(source="journal", where="the journal (" + ", ".join(sorted(set(UNITS))) + ")",
                entries=entries, next_cursor=cursor)


def page(**kwargs) -> Page:
    """This install's log: launchd's file on a Mac, else the journal under
    systemd, else LogUnavailableError."""
    named = os.environ.get("LUMIVERB_LOG_FILE")
    if named:
        return file_page(Path(named), **kwargs)
    if os.environ.get("INVOCATION_ID") and shutil.which("journalctl"):
        return journal_page(**kwargs)
    raise LogUnavailableError("Lumiverb was started from a terminal: its log is there.")
