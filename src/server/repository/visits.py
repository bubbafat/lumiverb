"""Visits (ADR-025): today's visits a visitor at a time, every day before today as totals, and the reports.
src/server/visits.py has the rules (who a visitor is, the window, bots); this module only the SQL."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import text
from sqlmodel import Session

# What a report adds up, in the order it says them.
ACTIONS = ("view", "open", "play", "export", "demo")
MOST_CLIPS = 20  # a report's clips most opened and played
MOST_SOURCES = 20  # a report's sites visitors came from

# Today's and every day before today's visits as one set of totals (by day, place, action and clip): today's
# added up as upkeep will.
_ALL_DAYS = """
    SELECT day, place, place_id, action, clip_id, visitors, times, bots FROM visit_days
    UNION ALL
    SELECT day, place, place_id, action, clip_id,
           count(*) FILTER (WHERE visitor <> 'bot'),
           coalesce(sum(times) FILTER (WHERE visitor <> 'bot'), 0),
           coalesce(sum(times) FILTER (WHERE visitor = 'bot'), 0)
    FROM visits_today GROUP BY day, place, place_id, action, clip_id
"""
_ALL_SOURCES = """
    SELECT day, place, place_id, came_from, visitors FROM visit_sources
    UNION ALL
    SELECT day, place, place_id, came_from, count(*) FROM visits_today
    WHERE action = 'view' AND came_from <> '' AND visitor <> 'bot'
    GROUP BY day, place, place_id, came_from
"""


@dataclass
class Counted:
    """Visitors (each day's added up) and times."""

    visitors: int = 0
    times: int = 0

    def add(self, visitors: int, times: int) -> None:
        self.visitors += visitors
        self.times += times


@dataclass
class Report:
    """A place's visits over some days: each day's by action, the totals, its clips most opened and played, the
    sites visitors came from, and bots."""

    days: dict[date, dict[str, Counted]] = field(default_factory=dict)
    totals: dict[str, Counted] = field(default_factory=lambda: {a: Counted() for a in ACTIONS})
    clips: list[tuple[str, int, int]] = field(default_factory=list)  # (clip, opened, played) times
    sources: list[tuple[str, int]] = field(default_factory=list)  # (site, visitors)
    bots: int = 0


@dataclass
class PlaceTotals:
    place: str
    place_id: str
    name: str | None
    totals: dict[str, Counted]
    bots: int


class VisitRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def salt(self, day: date) -> bytes:
        """The day's salt, made by the first visit of the day."""
        self._session.execute(text("INSERT INTO visit_salts (day, salt) VALUES (:day, :salt) ON CONFLICT DO NOTHING"),
                              {"day": day, "salt": secrets.token_bytes(32)})
        return bytes(self._session.execute(text("SELECT salt FROM visit_salts WHERE day = :day"),
                                           {"day": day}).scalar_one())

    def count(self, *, day: date, place: str, place_id: str, action: str, clip_id: str, visitor: str,
              now: datetime, window_start: datetime | None, came_from: str) -> None:
        """A visit: a new row for the visitor, or one more time unless their last was after window_start (None:
        every time counts). Where they came from is kept from their first visit that says."""
        self._session.execute(text("""
            INSERT INTO visits_today AS v (day, place, place_id, action, clip_id, visitor, times, last_at, came_from)
            VALUES (:day, :place, :place_id, :action, :clip_id, :visitor, 1, :now, :came_from)
            ON CONFLICT (day, place, place_id, action, clip_id, visitor) DO UPDATE SET
                times = v.times + CASE WHEN CAST(:window_start AS timestamptz) IS NOT NULL
                                            AND v.last_at > CAST(:window_start AS timestamptz) THEN 0 ELSE 1 END,
                last_at = GREATEST(v.last_at, EXCLUDED.last_at),
                came_from = CASE WHEN v.came_from = '' THEN EXCLUDED.came_from ELSE v.came_from END
        """), {"day": day, "place": place, "place_id": place_id, "action": action, "clip_id": clip_id,
               "visitor": visitor, "now": now, "window_start": window_start, "came_from": came_from})

    def add_up(self, before: date) -> int:
        """Every day before `before` as totals, its visitors and its salt deleted (one transaction, the caller's
        to commit). How many visitors' rows were added up."""
        params = {"before": before}
        self._session.execute(text("""
            INSERT INTO visit_days AS d (day, place, place_id, action, clip_id, visitors, times, bots)
            SELECT day, place, place_id, action, clip_id,
                   count(*) FILTER (WHERE visitor <> 'bot'),
                   coalesce(sum(times) FILTER (WHERE visitor <> 'bot'), 0),
                   coalesce(sum(times) FILTER (WHERE visitor = 'bot'), 0)
            FROM visits_today WHERE day < :before GROUP BY day, place, place_id, action, clip_id
            ON CONFLICT (day, place, place_id, action, clip_id) DO UPDATE SET
                visitors = d.visitors + EXCLUDED.visitors, times = d.times + EXCLUDED.times,
                bots = d.bots + EXCLUDED.bots
        """), params)
        self._session.execute(text("""
            INSERT INTO visit_sources AS s (day, place, place_id, came_from, visitors)
            SELECT day, place, place_id, came_from, count(*) FROM visits_today
            WHERE day < :before AND action = 'view' AND came_from <> '' AND visitor <> 'bot'
            GROUP BY day, place, place_id, came_from
            ON CONFLICT (day, place, place_id, came_from) DO UPDATE SET visitors = s.visitors + EXCLUDED.visitors
        """), params)
        added = self._session.execute(text("DELETE FROM visits_today WHERE day < :before"), params).rowcount
        self._session.execute(text("DELETE FROM visit_salts WHERE day < :before"), params)
        return added

    def forget(self, place: str, place_ids: list[str]) -> None:
        """The visits of places deleted for good."""
        if not place_ids:
            return
        for table in ("visits_today", "visit_days", "visit_sources"):
            self._session.execute(text(f"DELETE FROM {table} WHERE place = :place AND place_id = ANY(:ids)"),
                                  {"place": place, "ids": list(place_ids)})

    def report(self, place: str, place_id: str, since: date) -> Report:
        """A place's visits from `since` on."""
        params = {"place": place, "place_id": place_id, "since": since}
        out = Report()
        rows = self._session.execute(text(f"""
            SELECT day, action, sum(visitors) AS visitors, sum(times) AS times, sum(bots) AS bots
            FROM ({_ALL_DAYS}) v WHERE place = :place AND place_id = :place_id AND day >= :since
            GROUP BY day, action ORDER BY day
        """), params).all()
        for r in rows:
            if r.action not in out.totals:
                continue
            out.days.setdefault(r.day, {a: Counted() for a in ACTIONS})[r.action].add(int(r.visitors), int(r.times))
            out.totals[r.action].add(int(r.visitors), int(r.times))
            out.bots += int(r.bots)
        out.clips = [(r.clip_id, int(r.opened), int(r.played)) for r in self._session.execute(text(f"""
            SELECT clip_id, coalesce(sum(times) FILTER (WHERE action = 'open'), 0) AS opened,
                   coalesce(sum(times) FILTER (WHERE action = 'play'), 0) AS played
            FROM ({_ALL_DAYS}) v
            WHERE place = :place AND place_id = :place_id AND day >= :since AND clip_id <> ''
            GROUP BY clip_id ORDER BY sum(times) DESC, clip_id LIMIT {MOST_CLIPS}
        """), params)]
        out.sources = [(r.came_from, int(r.visitors)) for r in self._session.execute(text(f"""
            SELECT came_from, sum(visitors) AS visitors FROM ({_ALL_SOURCES}) s
            WHERE place = :place AND place_id = :place_id AND day >= :since
            GROUP BY came_from ORDER BY visitors DESC, came_from LIMIT {MOST_SOURCES}
        """), params)]
        return out

    def places(self, since: date) -> list[PlaceTotals]:
        """Every place with visits from `since` on, with its name (a project's or a library's; None for one
        deleted, the account's own for its demos) and totals, most visited first."""
        totals: dict[tuple[str, str], PlaceTotals] = {}
        for r in self._session.execute(text(f"""
            SELECT v.place, v.place_id, coalesce(p.name, l.name) AS name, v.action,
                   sum(v.visitors) AS visitors, sum(v.times) AS times, sum(v.bots) AS bots
            FROM ({_ALL_DAYS}) v
            LEFT JOIN projects p ON v.place = 'project' AND p.project_id = v.place_id
            LEFT JOIN libraries l ON v.place = 'library' AND l.library_id = v.place_id
            WHERE v.day >= :since
            GROUP BY v.place, v.place_id, coalesce(p.name, l.name), v.action
        """), {"since": since}):
            key = (r.place, r.place_id)
            if key not in totals:
                totals[key] = PlaceTotals(r.place, r.place_id, r.name, {a: Counted() for a in ACTIONS}, 0)
            if r.action in totals[key].totals:
                totals[key].totals[r.action].add(int(r.visitors), int(r.times))
            totals[key].bots += int(r.bots)
        return sorted(totals.values(), key=lambda t: (-sum(c.times for c in t.totals.values()), t.place, t.place_id))
