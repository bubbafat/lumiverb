"""Each day's site visits, kept in the control database (src/server/lifecycle/site_visits.py adds a day up from
nginx's log; upkeep keeps it, `_keep_site_visits`): counts and likely people, never an address."""

from __future__ import annotations

import json
from datetime import date

from sqlalchemy import text
from sqlmodel import Session

from src.server.lifecycle.site_visits import Day


class SiteVisitRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def kept_days(self) -> set[date]:
        return {row[0] for row in self._session.execute(text("SELECT day FROM site_visit_days"))}

    def keep(self, day: Day) -> bool:
        """The day, once: False if it was kept already (another upkeep's). Doesn't commit."""
        kept = self._session.execute(
            text("INSERT INTO site_visit_days (day, browsers, sources) VALUES (:d, CAST(:b AS jsonb),"
                 " CAST(:s AS jsonb)) ON CONFLICT (day) DO NOTHING RETURNING day"),
            {"d": day.day, "b": json.dumps(day.browsers), "s": json.dumps(day.sources)}).first()
        if kept is None:
            return False
        for person in day.people:
            self._session.execute(
                text("INSERT INTO site_visit_people (day, first_at, sites, device, why)"
                     " VALUES (:d, :at, CAST(:sites AS jsonb), :device, CAST(:why AS jsonb))"),
                {"d": day.day, "at": person.first_at, "sites": json.dumps(list(person.sites)),
                 "device": person.device, "why": json.dumps(list(person.why))})
        return True

    def since(self, first: date) -> list[dict]:
        """Each kept day from `first` on, newest first, with its likely people (first seen first)."""
        days = self._session.execute(text(
            "SELECT day, browsers, sources FROM site_visit_days WHERE day >= :f ORDER BY day DESC"),
            {"f": first}).mappings().all()
        people: dict[date, list[dict]] = {}
        for row in self._session.execute(text(
                "SELECT day, first_at, sites, device, why FROM site_visit_people WHERE day >= :f"
                " ORDER BY day, first_at, id"), {"f": first}).mappings():
            people.setdefault(row["day"], []).append(
                {"first_at": row["first_at"], "sites": row["sites"], "device": row["device"], "why": row["why"]})
        return [{"day": d["day"], "browsers": d["browsers"], "sources": d["sources"], "people": people.get(d["day"], [])}
                for d in days]
