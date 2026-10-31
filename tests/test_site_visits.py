"""Who other than the people running a server looked at its sites, from nginx's log
(src/server/lifecycle/site_visits.py, `lumiverb-server site-visits`): the sites read from nginx's own files, a
browser told from a scanner by what it loads, a likely person by what they did, the people running it left out, and
a line from before the log named the site placed by the file it asked for."""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from src.server.lifecycle import site_visits

pytestmark = pytest.mark.fast

IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
          "Version/18.0 Mobile/15E148 Safari/604.1")
MAC_CHROME = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0.0.0 Safari/537.36")
HEADLESS = MAC_CHROME.replace("Chrome/", "HeadlessChrome/")
GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"


def _sites(tmp_path: Path) -> Path:
    """deploy-web.sh's and deploy-site.sh's sites as certbot leaves them, and the folders they serve."""
    available = tmp_path / "sites-available"
    available.mkdir(exist_ok=True)
    front, docs = tmp_path / "site", tmp_path / "docs-site"
    for folder, files in ((front, ("index.html", "style.css", "site.js")),
                          (docs, ("index.html", "docs.css", "docs.js", *(f"page{n}.html" for n in range(1, 21))))):
        folder.mkdir(exist_ok=True)
        for name in files:
            (folder / name).write_text("x")
    redirect = ("server {{\n    if ($host = {0}) {{\n        return 301 https://$host$request_uri;\n    }}\n"
                "    listen 80;\n    server_name {0};\n    return 404;\n}}\n")
    (available / "lumiverb").write_text(
        "server {\n    server_name try.example.org;\n    root /opt/lumiverb/src/ui/web/dist;\n    listen 443 ssl;\n}\n"
        + redirect.format("try.example.org"))
    (available / "lumiverb-site").write_text(
        f"server {{\n    listen 80;\n    server_name example.org www.example.org;\n    root {front};\n}}\n")
    (available / "lumiverb-docs-site").write_text(
        f"server {{\n    listen 80;\n    server_name docs.example.org;\n    root {docs};\n}}\n")
    (available / "lumiverb-default").write_text("server {\n    server_name _;\n    return 444;\n}\n")
    (available / "lumiverb.before-update").write_text("server {\n    server_name old.example.org;\n    root /x;\n}\n")
    return available


def _line(address: str, at: str, request: str, status: int = 200, *, referrer: str = "-", agent: str = IPHONE,
          host: str | None = "try.example.org") -> str:
    named = f' "{host}"' if host is not None else ""
    return (f'{address} - - [{at} +0000] "{request} HTTP/2.0" {status} 512 "{referrer}" "{agent}"{named}\n')


def _log() -> list[str]:
    lines = []
    # A person: the front page, a link from it to the demo, the demo started, two clips opened.
    p = "203.0.113.5"
    lines += [_line(p, "21/Oct/2026:10:14:00", "GET /", host="example.org"),
              _line(p, "21/Oct/2026:10:14:01", "GET /style.css", host="www.example.org"),
              _line(p, "21/Oct/2026:10:15:00", "GET /", referrer="https://example.org/"),
              _line(p, "21/Oct/2026:10:15:01", "GET /assets/index-abc123.js"),
              _line(p, "21/Oct/2026:10:15:05", "POST /v1/auth/demo"),
              _line(p, "21/Oct/2026:10:15:20", "GET /v1/assets/a1?x=1"),
              _line(p, "21/Oct/2026:10:16:20", "GET /v1/assets/a2")]
    # A reader from Reddit: three of the docs' pages over five minutes.
    r = "198.51.100.7"
    lines += [_line(r, "29/Oct/2026:15:10:00", "GET /page1.html", referrer="https://www.reddit.com/r/x",
                    agent=MAC_CHROME, host="docs.example.org"),
              _line(r, "29/Oct/2026:15:10:01", "GET /docs.css", agent=MAC_CHROME, host="docs.example.org"),
              _line(r, "29/Oct/2026:15:13:00", "GET /page2.html", agent=MAC_CHROME, host="docs.example.org"),
              _line(r, "29/Oct/2026:15:15:00", "GET /page3.html", agent=MAC_CHROME, host="docs.example.org")]
    # A crawler in a real browser taking the whole docs site: a browser, no person.
    c = "192.0.2.77"
    lines += [_line(c, "29/Oct/2026:16:00:00", "GET /docs.css", agent=MAC_CHROME, host="docs.example.org")]
    lines += [_line(c, f"29/Oct/2026:16:{n:02}:00", f"GET /page{n}.html", agent=MAC_CHROME, host="docs.example.org")
              for n in range(1, 21)]
    # Scanners: a headless browser, a crawler, a probe answered 404, someone else's domain, a fake Google referrer
    # from an address that never loads a page's files.
    lines += [_line("192.0.2.80", "22/Oct/2026:01:00:00", "GET /assets/index-abc123.js", agent=HEADLESS),
              _line("66.249.66.1", "22/Oct/2026:01:00:00", "GET /assets/index-abc123.js", agent=MAC_CHROME),
              _line("192.0.2.81", "22/Oct/2026:01:00:00", "GET /assets/index-abc123.js", agent=GOOGLEBOT),
              _line("192.0.2.82", "22/Oct/2026:01:00:00", "GET /.env", 404, agent=MAC_CHROME),
              _line("192.0.2.83", "22/Oct/2026:01:00:00", "GET /assets/index-abc123.js", agent=MAC_CHROME,
                    host="mail.someone-else.example"),
              _line("192.0.2.84", "22/Oct/2026:01:00:00", "GET /", referrer="https://www.google.com/",
                    agent=MAC_CHROME)]
    # The people running it: the tunnel, an SSH login's address, one the install names.
    for me in ("127.0.0.1", "192.0.2.10", "192.0.2.20"):
        lines += [_line(me, "30/Oct/2026:09:00:00", "GET /assets/index-abc123.js"),
                  _line(me, "30/Oct/2026:09:00:05", "POST /v1/auth/demo")]
    # From before the log named the site: placed by the file it asked for.
    o = "198.51.100.9"
    lines += [_line(o, "20/Oct/2026:12:00:00", "GET /docs.css", agent=MAC_CHROME, host=None),
              _line(o, "20/Oct/2026:12:00:01", "GET /page4.html", agent=MAC_CHROME, host=None),
              _line(o, "20/Oct/2026:12:03:00", "GET /page5.html", agent=MAC_CHROME, host=None),
              _line(o, "20/Oct/2026:12:06:00", "GET /page6.html", agent=MAC_CHROME, host=None)]
    # A browser and nothing more: counted as one, not as a person.
    lines += [_line("198.51.100.30", "25/Oct/2026:08:00:00", "GET /assets/index-abc123.js", agent=MAC_CHROME)]
    # From before the log named the site (the demo server's, Oct 20-29): "/" placed by the file loaded right after
    # it. A reload inside the web app follows nothing; the front page's link to it does.
    lines += [_line("198.51.100.40", "22/Oct/2026:10:00:00", "GET /", referrer="https://try.example.org/",
                    agent=MAC_CHROME, host=None),
              _line("198.51.100.40", "22/Oct/2026:10:00:01", "GET /assets/index-abc123.js", agent=MAC_CHROME,
                    host=None),
              _line("198.51.100.41", "23/Oct/2026:10:00:00", "GET /", referrer="https://example.org/",
                    agent=MAC_CHROME, host=None),
              _line("198.51.100.41", "23/Oct/2026:10:00:02", "GET /assets/index-abc123.js", agent=MAC_CHROME,
                    host=None)]
    # ...and a docs page linked from the docs is the docs' own navigation.
    lines += [_line("198.51.100.42", "24/Oct/2026:10:00:00", "GET /docs.css", agent=MAC_CHROME, host=None),
              _line("198.51.100.42", "24/Oct/2026:10:00:01", "GET /page7.html", referrer="https://docs.example.org/",
                    agent=MAC_CHROME, host=None)]
    # A reader skimming six pages in under a minute, 5 to 24 s apart (the demo server's), and a crawler taking five
    # in a burst.
    for n, at in enumerate(("15:13:27", "15:13:35", "15:13:40", "15:14:04", "15:14:13", "15:14:24"), start=8):
        lines.append(_line("198.51.100.43", f"26/Oct/2026:{at}", f"GET /page{n}.html", agent=IPHONE,
                           host="docs.example.org"))
    lines.append(_line("198.51.100.43", "26/Oct/2026:15:13:28", "GET /docs.css", agent=IPHONE,
                       host="docs.example.org"))
    lines.append(_line("198.51.100.44", "27/Oct/2026:09:00:00", "GET /docs.css", agent=MAC_CHROME,
                       host="docs.example.org"))
    lines += [_line("198.51.100.44", "27/Oct/2026:09:00:01", f"GET /page{n}.html", agent=MAC_CHROME,
                    host="docs.example.org") for n in range(14, 19)]
    # Certbot's redirect answers every path with a 301: no file given, so no browser.
    lines += [_line("198.51.100.45", "28/Oct/2026:01:00:00", "GET /assets/config.js", 301, agent=MAC_CHROME,
                    host="example.org"),
              _line("198.51.100.45", "28/Oct/2026:01:00:01", "GET /assets/index-abc123.js", 301, agent=MAC_CHROME)]
    return lines


def test_the_sites_are_nginx_s_own(tmp_path):
    found = {site.name: site for site in site_visits.sites(_sites(tmp_path))}
    assert sorted(found) == ["docs.example.org", "example.org", "try.example.org"], "the default site and backups aside"
    assert found["try.example.org"].app and not found["example.org"].app
    assert found["example.org"].names == {"example.org", "www.example.org"}
    assert "/page1.html" in found["docs.example.org"].pages and "/index.html" not in found["docs.example.org"].pages


def test_a_web_app_answering_any_name_takes_every_name_but_the_others(tmp_path):
    available = tmp_path / "sites-available"
    available.mkdir()
    (available / "lumiverb").write_text("server {\n    listen 80 default_server;\n    server_name _;\n    root /x;\n}\n")
    (app,) = site_visits.sites(available)
    assert (app.name, app.names, app.app) == ("the web app", frozenset(), True)
    visitors, _ = site_visits.read([_line("203.0.113.9", "21/Oct/2026:10:00:00", "GET /assets/a.js",
                                          host="192.168.1.20")], [app])
    assert visitors["203.0.113.9"].sites == {"the web app"}


def _report(tmp_path, **kwargs) -> str:
    return site_visits.report(_log(), site_visits.sites(_sites(tmp_path)), today=date(2026, 10, 31),
                              not_counted=["192.0.2.20"], ours=lambda since: {"192.0.2.10"}, **kwargs)


def test_the_likely_people_and_what_they_did(tmp_path):
    said = _report(tmp_path)
    assert "Visitors other than the people running this server, Oct 20 to Oct 31" in said
    people = said.split("Likely people: ", 1)[1].split("\n\n", 1)[0].splitlines()
    assert people[0] == "5"
    assert people[1].split() == ["Oct", "20", "12:00", "docs.example.org", "Mac", "Chrome", "read", "3",
                                 "docs.example.org", "pages"], "a line from before the format, by its file"
    assert people[2].startswith("  Oct 21 10:14  example.org+try.example.org")
    assert "iPhone Safari" in people[2]
    assert people[2].endswith("tried the demo x1; opened 2 clips; followed a link from another of these sites")
    assert people[3].startswith("  Oct 23 10:00  try.example.org")
    assert people[3].endswith("followed a link from another of these sites"), "the front page's link, by its file"
    assert people[4].startswith("  Oct 26 15:13  docs.example.org") and people[4].endswith("read 6 docs.example.org pages")
    assert people[5].endswith("from reddit.com; read 3 docs.example.org pages")
    assert "Came from: reddit.com 1" in said, "a Google referrer from a scanner isn't counted"


def test_browsers_are_counted_apart_and_scanners_not_at_all(tmp_path):
    said = _report(tmp_path)
    browsers = dict(line.split(None, 2)[:2] for line in
                    said.split("Browser-like addresses", 1)[1].split("\n\n", 1)[0].splitlines()[1:])
    # The web app: the person, the browser alone and the two placed by their file; the docs: the readers, both
    # crawlers and the ones from before.
    assert browsers == {"try.example.org": "4", "example.org": "1", "docs.example.org": "6"}
    assert "Left out as the people running it: 127.0.0.1, 192.0.2.10, 192.0.2.20, ::1" in said
    every = _report(tmp_path, verbose=True).split("Every browser-like address:", 1)[1]
    for scanner in ("192.0.2.80", "66.249.66.1", "192.0.2.81", "192.0.2.82", "192.0.2.83", "192.0.2.84",
                    "198.51.100.45"):
        assert scanner not in every, scanner
    assert "192.0.2.77" in every and "198.51.100.30" in every


def test_the_last_days_only(tmp_path):
    said = _report(tmp_path, days=3)
    assert "Oct 29 to Oct 31" in said and "Likely people: 1\n" in said


def test_the_ssh_logins_are_read_from_the_journal(monkeypatch):
    journal = ("Oct 30 09:00:00 try sshd[1]: Accepted publickey for root from 192.0.2.10 port 50000 ssh2: ED25519\n"
               "Oct 30 09:05:00 try sshd[2]: Failed password for root from 192.0.2.99 port 1 ssh2\n"
               "Oct 30 10:00:00 try sshd[3]: Accepted publickey for robert from 2001:db8::5 port 2 ssh2\n")

    def run(cmd, **kwargs):
        assert cmd[:5] == ["journalctl", "-u", "ssh", "-u", "sshd"] and cmd[6] == "2026-10-19"
        return subprocess.CompletedProcess(cmd, 0, journal, "")

    monkeypatch.setattr(site_visits.subprocess, "run", run)
    assert site_visits.ssh_logins(date(2026, 10, 20)) == {"192.0.2.10", "2001:db8::5"}


def test_the_command_says_when_there_s_no_site(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(site_visits, "sites", lambda available=None: [])
    assert site_visits.run() == 1
    assert "No Lumiverb site" in capsys.readouterr().err


def test_it_runs_on_the_standard_library_alone(tmp_path):
    out = subprocess.run([sys.executable, "-I", "-S", site_visits.__file__, "--help"], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0 and "--days" in out.stdout, out.stderr


@pytest.mark.parametrize("argv", [["site-visits", "--env", "{env}", "--days", "7", "-v"],
                                  ["--env", "{env}", "site-visits", "--days", "7", "-v"]])
def test_the_command_leaves_out_what_the_env_file_names(tmp_path, monkeypatch, argv):
    from src.server.lifecycle import cli

    env = tmp_path / "env"
    env.write_text(f"{site_visits.NOT_COUNTED}=192.0.2.20 2001:db8::7\n")
    called = {}
    monkeypatch.setattr(site_visits, "run", lambda **kwargs: called.update(kwargs) or 0)
    assert cli.main([part.replace("{env}", str(env)) for part in argv]) == 0
    assert {**called, "not_counted": list(called["not_counted"])} == {
        "days": 7, "verbose": True, "not_counted": ["192.0.2.20", "2001:db8::7"]}


def test_each_whole_day_is_added_up_once_and_the_first_is_left_out(tmp_path):
    here = site_visits.sites(_sites(tmp_path))
    added = site_visits.days(_log, here, before=date(2026, 10, 30), not_counted=["192.0.2.20"],
                             ours=lambda since: {"192.0.2.10"})
    by_day = {day.day: day for day in added}
    assert min(by_day) == date(2026, 10, 21), "the log's first day may begin partway"
    assert max(by_day) == date(2026, 10, 29), "today's isn't whole yet"
    oct21 = by_day[date(2026, 10, 21)]
    (person,) = oct21.people
    assert person.sites == ("example.org", "try.example.org") and person.device == "iPhone Safari"
    assert person.why == ("tried the demo x1", "opened 2 clips", "followed a link from another of these sites")
    assert oct21.browsers == {"try.example.org": 1, "example.org": 1, "docs.example.org": 0}
    oct29 = by_day[date(2026, 10, 29)]
    assert [p.why for p in oct29.people] == [("from reddit.com", "read 3 docs.example.org pages")]
    assert oct29.sources == {"reddit.com": 1} and oct29.browsers["docs.example.org"] == 2, "the reader, the crawler"
    assert by_day[date(2026, 10, 22)].people == () and by_day[date(2026, 10, 22)].browsers["try.example.org"] == 1

    again = site_visits.days(_log, here, before=date(2026, 10, 30), kept=by_day, ours=lambda since: set())
    assert again == [], "a day kept is done"
