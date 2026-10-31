"""Who, other than the people running this server, looked at its sites: the web app (a demo's, a public page's)
and deploy-site.sh's static sites (lumiverb.io's front page, the docs), from nginx's access log. Each line names its
site (site_update.py's lumiverb_host format); a line from before it is placed by the file it asked for. The demo
server's hand-made `webstats` (Oct 29-31, 2026) made into Lumiverb's own: `lumiverb-server site-visits`, run as root
(nginx's log and the SSH journal are root's to read). The standard library alone.

Left out as the people running it: loopback (an admin's tunnel), this machine's own addresses, every address someone
signed in from over SSH in that time (the journal), and any the install names (the env file's
LUMIVERB_SITE_VISITS_NOT_COUNTED, addresses separated by spaces).

Most requests are scanners and crawlers. A browser: an address answered 2xx or 3xx, with a user agent that isn't a
bot's or a script's, outside the search engines' crawler networks, that was given one of a site's own files (its
script or style: a 200 or 304, never certbot's 301 that answers every path): an upper bound, as many scanners run a real browser. A likely person: a browser that did what a
person does: started the demo, opened a clip, came from a search or social site, followed a link from another of
these sites, or read a few of a static site's pages, seconds apart (more, or in a burst, is a crawler). An
address isn't a person: a phone on a mobile network changes address, and several people can share one.

nginx keeps its log 14 days (logrotate): the report covers what's there. Times are the log's, this server's.
"""

from __future__ import annotations

import argparse
import gzip
import os
import re
import subprocess
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

LOGS = "/var/log/nginx/access.log*"
SITES = "/etc/nginx/sites-available"
APP_SITE = "lumiverb"  # deploy-web.sh's; deploy-site.sh's are lumiverb-<folder>
NOT_COUNTED = "LUMIVERB_SITE_VISITS_NOT_COUNTED"

# nginx's combined format, and "$host" after it when the site wrote it (site_update.py's lumiverb_host).
_LINE = re.compile(r'(\S+) \S+ \S+ \[([^\]]+)\] "(\S+) (\S+)[^"]*" (\d{3}) \S+ "([^"]*)" "([^"]*)"(?: "([^"]*)")?')
# What a crawler, a link preview, a script or a scanner says it is (visits.py's, and the scanners and the search
# engines' and AI crawlers the demo server's log showed).
_BOT = re.compile(r"bot|crawl|spider|slurp|preview|facebookexternalhit|headless|lighthouse|curl|wget|python|httpx"
                  r"|go-http-client|okhttp|axios|node-fetch|scrapy|libwww|java/|zgrab|masscan|nmap|censys|expanse"
                  r"|google-|googleother|semrush|ahrefs|petal|yandex|baidu|bytespider|gptbot|claude", re.IGNORECASE)
CRAWLER_NETS = ("66.249.", "157.55.", "207.46.", "40.77.", "17.241.", "17.246.")  # Google's, Bing's, Apple's
# Where a person comes from: a search engine or a social site (by the referrer's host), or an Android app.
_SOCIAL = re.compile(r"(^|\.)(google|bing|duckduckgo|yahoo|ecosia|kagi|brave|startpage|reddit|ycombinator|twitter|x"
                     r"|t|facebook|instagram|linkedin|github|mastodon|bsky|threads|youtube)\.|^android-app\Z",
                     re.IGNORECASE)
_CLIP = re.compile(r"/v1/assets/[^/]+")
_SSH_LOGIN = re.compile(r"Accepted \S+ for \S+ from (\S+)")
PAGES_READ = (3, 15)  # a static site's pages: a few is a reader; more is a crawler taking the whole site
# A reader takes seconds between pages (the demo server's skimmed six in 57 s, 5 to 24 s apart); a crawler asks for
# them in a burst: the median gap between a site's page requests says which.
READING_GAP = timedelta(seconds=5)
# A file a browser was given (a 301, certbot's redirect answering every path, isn't one).
GIVEN = ("200", "304")
# A line from before the log named the site, asking for "/": the site whose own file the address loads next, a
# browser loading the page's script or style right after the page.
NEXT_FILE_WITHIN = timedelta(seconds=10)


@dataclass(frozen=True)
class Site:
    name: str  # what the report calls it: its first server name ("the web app" for one answering any name)
    names: frozenset[str]
    root: Path | None
    app: bool  # the web app's (deploy-web.sh's), else a static site of deploy-site.sh's
    pages: frozenset[str] = frozenset()  # a static site's pages ("/docs.html"), its front page aside

    def own_file(self, path: str) -> bool:
        """One of the page's own files, which a browser showing it loads (a scanner asking for pages doesn't)."""
        if self.app:
            return path.startswith("/assets/") and path.endswith(".js")
        return path.endswith((".css", ".js")) and self.root is not None and (self.root / path.lstrip("/")).is_file()


def _blocks(text: str) -> Iterable[list[str]]:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.fullmatch(r"(\s*)server\s*\{\s*", line)
        if m:
            end = next((k for k in range(i + 1, len(lines)) if lines[k] == f"{m.group(1)}}}"), len(lines))
            yield lines[i:end]


def sites(available: Path = Path(SITES)) -> list[Site]:
    """This server's Lumiverb sites, from nginx's: each one's names and folder, in the blocks that serve its files."""
    found: list[Site] = []
    files = [available / APP_SITE, *sorted(available.glob(f"{APP_SITE}-*"))]
    for path in files:
        if path.name == f"{APP_SITE}-default" or path.suffix in (".part",) or path.name.endswith(".before-update"):
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        for block in _blocks(text):
            said = {k: v for k, v in (line.strip().rstrip(";").split(None, 1) for line in block
                                      if line.strip().startswith(("server_name ", "root ")))}
            if "root" not in said or "server_name" not in said:
                continue
            names = [n.lower() for n in said["server_name"].split()]
            root = Path(said["root"])
            app = path.name == APP_SITE
            pages = frozenset() if app else frozenset(
                "/" + p.relative_to(root).as_posix() for p in root.rglob("*.html") if p.name != "index.html")
            found.append(Site(name=names[0] if names != ["_"] else "the web app", names=frozenset(names) - {"_"},
                              root=root, app=app, pages=pages))
            break
    return found


def device(user_agent: str) -> str:
    kind = next((k for k in ("iPhone", "iPad", "Android", "Macintosh", "Windows", "CrOS", "Linux") if k in user_agent),
                "?")
    kind = {"Macintosh": "Mac", "CrOS": "Chromebook"}.get(kind, kind)
    browser = next((b for k, b in (("CriOS", "Chrome"), ("FxiOS", "Firefox"), ("Edg", "Edge"), ("Firefox", "Firefox"),
                                   ("Chrome", "Chrome"), ("Safari", "Safari")) if k in user_agent), "")
    return f"{kind} {browser}".strip()


def referrer_host(referrer: str) -> str:
    if referrer in ("", "-"):
        return ""
    if referrer.startswith("android-app:"):
        return "android-app"
    return re.sub(r"^[a-z-]+://(www\.)?", "", referrer.lower()).split("/")[0].split(":")[0]


@dataclass
class Visitor:
    user_agent: str = ""
    times: list[datetime] = field(default_factory=list)
    sites: set[str] = field(default_factory=set)
    demos: int = 0
    clips: int = 0
    pages: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    read_at: dict[str, list[datetime]] = field(default_factory=lambda: defaultdict(list))  # each site's page requests
    came_from: set[str] = field(default_factory=set)
    followed: bool = False
    # "/" asked for from one of these sites' links on a line that doesn't name its site: (when, the link's site)
    unplaced: list[tuple[datetime, str]] = field(default_factory=list)

    def why(self) -> list[str]:
        """What it did that a person does, if anything."""
        said = []
        if self.demos:
            said.append(f"tried the demo x{self.demos}")
        if self.clips:
            said.append(f"opened {self.clips} clip{'s' if self.clips > 1 else ''}")
        if self.came_from:
            said.append("from " + ", ".join(sorted(self.came_from)))
        if self.followed:
            said.append("followed a link from another of these sites")
        for name, read in sorted(self.pages.items()):
            if PAGES_READ[0] <= len(read) <= PAGES_READ[1] and _median_gap(self.read_at[name]) >= READING_GAP:
                said.append(f"read {len(read)} {name} pages")
        return said


def _median_gap(times: list[datetime]) -> timedelta:
    ordered = sorted(times)
    gaps = sorted(b - a for a, b in zip(ordered, ordered[1:], strict=False))
    return gaps[len(gaps) // 2] if gaps else timedelta()


def _opened(paths: Iterable[str]) -> Iterable[str]:
    for path in sorted(paths, key=lambda p: (os.path.getmtime(p), p), reverse=False):
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", errors="replace") as lines:
            yield from lines


def read(lines: Iterable[str], here: list[Site], *, since: date = date.min) -> tuple[dict[str, Visitor], date | None]:
    """Every browser-like address in the log's lines, with what it did; and the first day the lines cover."""
    by_name = {name: site for site in here for name in site.names}
    any_name = next((site for site in here if site.app and not site.names), None)
    any_app = any(site.app for site in here)
    visitors: dict[str, Visitor] = defaultdict(Visitor)
    first: date | None = None
    for line in lines:
        m = _LINE.match(line)
        if not m:
            continue
        address, when, method, path, status, referrer, user_agent, host = m.groups()
        try:
            at = datetime.strptime(when, "%d/%b/%Y:%H:%M:%S %z")
        except ValueError:
            continue
        if at.date() < since:
            continue
        first = min(first or at.date(), at.date())
        if (not status.startswith(("2", "3")) or not user_agent.strip() or _BOT.search(user_agent)
                or address.startswith(CRAWLER_NETS)):
            continue
        path = path.split("?", 1)[0]
        named = host is not None
        if named:
            site = by_name.get(host.lower().rstrip("."), any_name)
            if site is None:
                continue  # asked for by address or another name: a scanner (v0.1.162's default site closes those)
        else:  # a line from before the format named the site: by the file it asked for
            site = next((s for s in here if s.own_file(path)), None)
        visitor = visitors[address]
        visitor.user_agent = visitor.user_agent or user_agent
        came = referrer_host(referrer)
        if site is not None and site.own_file(path) and status in GIVEN:
            visitor.sites.add(site.name)
            visitor.times.append(at)
            for since_page, link in visitor.unplaced:  # the "/" just before was this site's
                if at - since_page <= NEXT_FILE_WITHIN and link not in site.names:
                    visitor.followed = True
            visitor.unplaced.clear()
        app = site.app if named else any_app
        if app and method == "POST" and path == "/v1/auth/demo":
            visitor.demos += 1
        if app and method == "GET" and _CLIP.fullmatch(path):
            visitor.clips += 1
        if came and _SOCIAL.search(came):
            visitor.came_from.add(came)
        # The page it opened, and whether from another of these sites' links: a line naming its site says which;
        # one that doesn't, by the static site whose page it is, or for "/" by the next file it loads.
        if named:
            page_of = site if path == "/" or path in site.pages else None
        else:
            of = [s for s in here if path in s.pages]
            page_of = of[0] if len(of) == 1 else None
            if path == "/" and came in by_name:
                visitor.unplaced.append((at, came))
        if page_of is not None:
            if path in page_of.pages:
                visitor.pages[page_of.name].add(path)
                visitor.read_at[page_of.name].append(at)
            if came in by_name and came not in page_of.names:
                visitor.followed = True
    return {a: v for a, v in visitors.items() if v.sites}, first


def machine_addresses() -> set[str]:
    try:
        said = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return set(said.split())


def ssh_logins(since: date) -> set[str]:
    """Every address someone signed in from over SSH since then (the day before: a session already open)."""
    try:
        said = subprocess.run(["journalctl", "-u", "ssh", "-u", "sshd", "--since", str(since - timedelta(days=1)),
                               "--no-pager", "-q"], capture_output=True, text=True, timeout=120).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return set(_SSH_LOGIN.findall(said))


def report(lines: Iterable[str], here: list[Site], *, today: date, days: int = 0, verbose: bool = False,
           not_counted: Iterable[str] = (), ours: Callable[[date], set[str]] | None = None) -> str:
    """The report `lumiverb-server site-visits` prints. `ours`: the addresses the people running the server used
    since a day (this machine's, SSH's); `not_counted`: the install's own list."""
    since = today - timedelta(days=days - 1) if days > 0 else date.min
    visitors, first = read(lines, here, since=since)
    left_out = {"127.0.0.1", "::1", *not_counted, *(ours or _ours)(first or today)}
    browsers = {a: v for a, v in visitors.items() if a not in left_out}
    people = sorted(((min(v.times), v, why) for v in browsers.values() if (why := v.why())), key=lambda p: p[0])

    out = [f"Visitors other than the people running this server, {first:%b %d} to {today:%b %d}"
           if first else "Visitors other than the people running this server: nothing in nginx's log yet",
           "(nginx keeps its log 14 days; an address isn't a person)", ""]
    out.append(f"Likely people: {len(people)}")
    for at, v, why in people:
        days_seen = len({t.date() for t in v.times})
        more = f" ({days_seen} days)" if days_seen > 1 else ""
        out.append(f"  {at:%b %d %H:%M}  {'+'.join(sorted(v.sites)):24} {device(v.user_agent):16} "
                   f"{'; '.join(why)}{more}")
    came: dict[str, set[str]] = defaultdict(set)
    for address, v in browsers.items():
        for host in v.came_from:
            came[host].add(address)
    if came:
        out += ["", "Came from: " + ", ".join(f"{h} {len(a)}" for h, a in sorted(came.items(),
                                                                                 key=lambda kv: (-len(kv[1]), kv[0])))]
    out += ["", "Browser-like addresses (at most: many scanners run a real browser):"]
    for site in here:
        seen = {a: v for a, v in browsers.items() if site.name in v.sites}
        by_day: dict[date, set[str]] = defaultdict(set)
        for address, v in seen.items():
            for t in v.times:
                by_day[t.date()].add(address)
        out.append(f"  {site.name:24} {len(seen):4}   " + ", ".join(f"{d:%b %d} {len(a)}"
                                                                    for d, a in sorted(by_day.items())))
    out += ["", "Left out as the people running it: " + ", ".join(sorted(left_out))]
    if verbose:
        out += ["", "Every browser-like address:"]
        for address, v in sorted(browsers.items(), key=lambda kv: min(kv[1].times)):
            out.append(f"  {min(v.times):%b %d %H:%M}  {address:39} {'+'.join(sorted(v.sites)):24} "
                       f"{v.user_agent[:100]}")
    return "\n".join(out) + "\n"


def _ours(since: date) -> set[str]:
    return machine_addresses() | ssh_logins(since)


def run(*, days: int = 0, verbose: bool = False, not_counted: Iterable[str] = ()) -> int:
    """The report, printed: `lumiverb-server site-visits`."""
    here = sites()
    if not here:
        print(f"✗ No Lumiverb site in {SITES}: site visits are counted from a Linux server's nginx "
              "(deploy-web.sh, deploy-site.sh)", file=sys.stderr)
        return 1
    import glob

    try:
        print(report(_opened(glob.glob(LOGS)), here, today=date.today(), days=days, verbose=verbose,
                     not_counted=not_counted), end="")
    except PermissionError:
        print("✗ nginx's log is root's to read: run it with sudo", file=sys.stderr)
        return 1
    return 0


def add_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--days", type=int, default=0, help="only the last N days (the whole log by default)")
    parser.add_argument("-v", "--verbose", action="store_true", help="also every browser-like address")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="site_visits.py", description=__doc__.split("\n\n")[0])
    add_options(parser)
    args = parser.parse_args(argv)
    return run(days=args.days, verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
