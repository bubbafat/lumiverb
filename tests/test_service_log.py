"""Admin › Logs' reader (src/server/service_log.py): a Mac's file, rotated,
read newest first a page at a time; Linux's journal through journalctl
(faked here: its paging flags as journalctl reads them)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.server import service_log
from src.server.service_log import LogUnavailableError, file_page, journal_page

pytestmark = pytest.mark.fast

LINES = [
    "lumiverb-server: postgres started (pid 1)",
    "postgres | 2026-10-11 21:53:02.609 EDT [1] LOG:  database system is ready to accept connections",
    "api | INFO:     Started server process [3]",
    "scheduler | 2026-10-11 21:53:04,711 INFO src.server.scheduler.service: scheduler: started",
    "api | 2026-10-11 21:53:23,440 INFO src.server.api.routers.system: Backup started (an admin's Back up now)",
    "scheduler | 2026-10-11 21:54:00,000 ERROR src.server.scheduler.runners: faces failed for IMG_1.jpg",
    "api | 2026-10-11 21:54:00,100 WARNING src.server.api.routers.upkeep: Trash purge failed for tenant t",
    "scheduler | Traceback (most recent call last):",
    'scheduler |   File "work.py", line 3, in run',
    "scheduler | ValueError: boom",
    "quickwit | 2026-10-11T21:55:00.000Z  WARN quickwit_indexing: slow merge",
]


def _log(tmp_path: Path, lines: list[str], name: str = "lumiverb.log") -> Path:
    path = tmp_path / name
    path.write_text("".join(line + "\n" for line in lines))
    return path


def _texts(page) -> list[str]:
    return [f"{e.part}: {e.text}" for e in page.entries]


def test_newest_first_each_said_by_whom_and_a_traceback_kept_with_its_record(tmp_path):
    page = file_page(_log(tmp_path, LINES))
    assert page.source == "file" and page.next_cursor is None
    assert _texts(page)[0] == "quickwit: 2026-10-11T21:55:00.000Z  WARN quickwit_indexing: slow merge"
    assert _texts(page)[-1] == "lumiverb-server: postgres started (pid 1)"
    failed = next(e for e in page.entries if "faces failed" in e.text)
    assert failed.part == "scheduler" and failed.level == "error"
    assert failed.text.splitlines()[1:] == ["Traceback (most recent call last):", '  File "work.py", line 3, in run',
                                            "ValueError: boom"], "the warning between them is the API's"
    assert len(page.entries) == 8, "11 lines, 3 of them the traceback"


def test_problems_only_are_warnings_and_errors(tmp_path):
    page = file_page(_log(tmp_path, LINES), problems=True)
    assert [(e.part, e.level) for e in page.entries] == [
        ("quickwit", "warning"), ("api", "warning"), ("scheduler", "error")]


def test_by_part_and_by_text_any_case(tmp_path):
    path = _log(tmp_path, LINES)
    assert {e.part for e in file_page(path, part="api").entries} == {"api"}
    assert [e.part for e in file_page(path, text="BACKUP STARTED").entries] == ["api"]
    assert [e.part for e in file_page(path, text="valueerror").entries] == ["scheduler"], "found in its traceback"


def _all_pages(path: Path, limit: int, **kwargs) -> list[list[str]]:
    pages, after = [], None
    for _ in range(100):
        page = file_page(path, after=after, limit=limit, **kwargs)
        pages.append(_texts(page))
        after = page.next_cursor
        if after is None:
            return pages
    raise AssertionError("never ended")


@pytest.mark.parametrize("block", [7, 64, 64 * 1024])
def test_pages_go_back_through_the_file_losing_and_repeating_nothing(tmp_path, monkeypatch, block):
    # Small blocks: lines split across the reads.
    monkeypatch.setattr(service_log, "BLOCK", block)
    path = _log(tmp_path, LINES)
    pages = _all_pages(path, limit=2)
    assert [t for page in pages for t in page] == _texts(file_page(path)), "a traceback never split by a page"
    assert len(pages) > 3


def test_a_full_page_reads_on_for_the_record_a_traceback_belongs_to(tmp_path):
    # The scheduler's traceback lines are newer than the API's warning: a page
    # of one is full at the warning, before the traceback's record is read.
    path = _log(tmp_path, LINES[5:10])
    first = file_page(path, limit=1)
    assert [e.part for e in first.entries] == ["api", "scheduler"]
    assert first.entries[1].text.splitlines()[-1] == "ValueError: boom"


def test_a_traceback_longer_than_a_page_reads_on_for_is_kept_still(tmp_path, monkeypatch):
    monkeypatch.setattr(service_log, "COMPLETE_LINES", 2)
    path = _log(tmp_path, LINES[5:10])
    seen = "\n".join(t for page in _all_pages(path, limit=1) for t in page)
    for line in ("Traceback (most recent call last):", "ValueError: boom", "faces failed", "Trash purge failed"):
        assert seen.count(line) == 1, line


def test_a_rotation_between_pages_neither_repeats_nor_skips(tmp_path):
    path = _log(tmp_path, [f"api | 2026-10-11 10:00:{n:02d},000 INFO x: line {n}" for n in range(10)])
    first = file_page(path, limit=4)
    assert [e.text.rsplit(" ", 1)[1] for e in first.entries] == ["9", "8", "7", "6"]
    path.rename(tmp_path / "lumiverb.log.1")  # run rotates, as RotatingFileHandler does
    _log(tmp_path, ["api | 2026-10-11 10:01:00,000 INFO x: line new"])
    rest = file_page(path, after=first.next_cursor, limit=100)
    assert [e.text.rsplit(" ", 1)[1] for e in rest.entries] == ["5", "4", "3", "2", "1", "0"]
    assert file_page(path, limit=1).entries[0].text.endswith("line new"), "the first page is the new file's"


def test_older_files_follow_the_current_one(tmp_path):
    path = _log(tmp_path, ["api | 2026-10-11 10:00:02,000 INFO x: two"])
    _log(tmp_path, ["api | 2026-10-11 10:00:01,000 INFO x: one"], "lumiverb.log.1")
    _log(tmp_path, ["api | 2026-10-11 10:00:00,000 INFO x: zero"], "lumiverb.log.2")
    assert [t for page in _all_pages(path, limit=1) for t in page] == [
        "api: 2026-10-11 10:00:02,000 INFO x: two", "api: 2026-10-11 10:00:01,000 INFO x: one",
        "api: 2026-10-11 10:00:00,000 INFO x: zero"]


def test_a_search_that_finds_little_answers_a_stretch_at_a_time(tmp_path, monkeypatch):
    monkeypatch.setattr(service_log, "SCAN_BYTES", 200)
    path = _log(tmp_path, ["api | 2026-10-11 10:00:00,000 INFO x: needle",
                           *[f"api | 2026-10-11 10:00:01,000 INFO x: hay {n}" for n in range(50)]])
    first = file_page(path, text="needle")
    assert first.entries == [] and first.next_cursor, "nothing in the newest stretch: go on from there"
    found = [t for page in _all_pages(path, limit=10, text="needle") for t in page]
    assert found == ["api: 2026-10-11 10:00:00,000 INFO x: needle"]


def test_a_cursor_not_of_this_log_is_refused(tmp_path):
    with pytest.raises(ValueError):
        file_page(_log(tmp_path, LINES), after="nonsense")


def test_no_file_yet_says_so(tmp_path):
    with pytest.raises(LogUnavailableError, match="isn't there yet"):
        file_page(tmp_path / "lumiverb.log")


# ---------------------------------------------------------------------------
# The journal
# ---------------------------------------------------------------------------


def _record(n: int, unit: str, message: str) -> dict:
    return {"__CURSOR": f"c{n}", "__REALTIME_TIMESTAMP": str(1_791_800_000_000_000 + n * 1_000_000),
            "_SYSTEMD_UNIT": unit, "MESSAGE": message, "PRIORITY": "6"}


JOURNAL = [  # oldest first, as the journal holds them
    _record(0, "lumiverb-api.service", "INFO:     Started server process [3]"),
    _record(1, "lumiverb-scheduler.service", "2026-10-11 21:54:00,000 ERROR src.x: faces failed"),
    _record(2, "lumiverb-scheduler.service", "Traceback (most recent call last):"),
    _record(3, "lumiverb-api.service", "2026-10-11 21:54:00,100 WARNING src.y: slow"),
    _record(4, "lumiverb-scheduler.service", "ValueError: boom"),
    _record(5, "lumiverb-upkeep.service", "Search sync: 3 clips"),
    _record(6, "lumiverb-quickwit.service", "2026-10-11T21:55:00.000Z  WARN quickwit: slow merge"),
]


def _journalctl(records: list[dict], calls: list[list[str]], *, stderr: str = "", returncode: int = 0):
    """journalctl -o json -r -n N [--after-cursor=C] --unit=...: the newest N
    older than C (with -r, --after-cursor walks back from C, C left out)."""

    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[:6] == ["journalctl", "--no-pager", "-o", "json", "-r", "-n"]
        n = int(argv[6])
        units = {a.split("=", 1)[1] for a in argv if a.startswith("--unit=")}
        after = next((a.split("=", 1)[1] for a in argv if a.startswith("--after-cursor=")), None)
        newest_first = [r for r in reversed(records) if r["_SYSTEMD_UNIT"] in units]
        if after:
            newest_first = newest_first[[r["__CURSOR"] for r in newest_first].index(after) + 1:]
        out = "".join(json.dumps(r) + "\n" for r in newest_first[:n])
        return SimpleNamespace(stdout=out, stderr=stderr, returncode=returncode)

    return run


def test_the_journal_newest_first_by_unit_a_traceback_with_its_record(monkeypatch):
    monkeypatch.setattr(service_log, "JOURNAL_BATCH", 2)  # several journalctl calls for one page
    calls: list[list[str]] = []
    page = journal_page(run=_journalctl(JOURNAL, calls))
    assert page.source == "journal" and page.next_cursor is None
    assert [(e.part, e.level) for e in page.entries] == [
        ("quickwit", "warning"), ("upkeep", "info"), ("api", "warning"), ("scheduler", "error"), ("api", "info")]
    failed = page.entries[3]
    assert failed.text.splitlines() == ["2026-10-11 21:54:00,000 ERROR src.x: faces failed",
                                        "Traceback (most recent call last):", "ValueError: boom"]
    assert failed.at == "2026-10-12T10:13:21+00:00"
    assert all(set(service_log.UNITS) <= {a.split("=", 1)[1] for a in call if a.startswith("--unit=")}
               for call in calls)


def test_journal_pages_lose_and_repeat_nothing(monkeypatch):
    monkeypatch.setattr(service_log, "JOURNAL_BATCH", 3)
    seen, after = [], None
    for _ in range(20):
        page = journal_page(after=after, limit=1, run=_journalctl(JOURNAL, []))
        seen += [e.text for e in page.entries]
        after = page.next_cursor
        if after is None:
            break
    joined = "\n".join(seen)
    for record in JOURNAL:
        assert joined.count(record["MESSAGE"]) == 1, record["MESSAGE"]


def test_a_service_user_that_cant_read_the_journal_is_told_how_and_what_it_opens(monkeypatch):
    hint = "Hint: You are currently not seeing messages from other users and the system."
    with pytest.raises(LogUnavailableError) as told:
        journal_page(run=_journalctl([], [], stderr=hint))
    assert "sudo usermod -aG systemd-journal lumiverb" in str(told.value)
    assert "the whole system journal" in str(told.value), "the grant is said for what it is"


def test_a_journalctl_that_fails_says_why():
    with pytest.raises(LogUnavailableError, match="Failed to open"):
        journal_page(run=_journalctl([], [], stderr="Failed to open journal", returncode=1))


# ---------------------------------------------------------------------------
# Which
# ---------------------------------------------------------------------------


def test_a_macs_api_reads_the_file_run_names(tmp_path, monkeypatch):
    monkeypatch.setenv("LUMIVERB_LOG_FILE", str(_log(tmp_path, LINES)))
    assert service_log.page().source == "file"


def test_under_systemd_the_journal(monkeypatch):
    monkeypatch.delenv("LUMIVERB_LOG_FILE", raising=False)
    monkeypatch.setenv("INVOCATION_ID", "abc")
    monkeypatch.setattr(service_log.shutil, "which", lambda name: "/usr/bin/journalctl")
    monkeypatch.setattr(service_log.subprocess, "run", _journalctl(JOURNAL, []))
    assert service_log.page().source == "journal"


def test_started_from_a_terminal_theres_nothing_here(monkeypatch):
    monkeypatch.delenv("LUMIVERB_LOG_FILE", raising=False)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    with pytest.raises(LogUnavailableError, match="terminal"):
        service_log.page()
