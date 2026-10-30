"""A named person's other names, as the account's AI machine knows them (Robert, Oct 30: name someone "Robert
Horvick" and a search for "Rob" finds him, with nothing to type in): for each first name in a person's name, up to
MOST names people with it are commonly called, asked once per name (a rename asks again) and kept in the account's
database (system_metadata). The built-in list (nicknames.py) still says the common English ones without asking; this
adds what it can't know (Giuseppe is Beppe). A search reads them with the people it understands (understand.py), a
person whose own name was typed ranking above one found by another of their names.

Asked by upkeep (every 5 minutes), never by a search, so a search never waits for a machine, and only where the
account lets searches ask its machines (Settings › AI › Search, the same switch as AI-assisted search: people control
their costs). A machine that's off or busy leaves the name to the next upkeep; a reply that isn't what was asked for
is kept as no other names, and the name isn't asked again.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable

from sqlalchemy import text
from sqlmodel import Session

from src.server.models.tenant import SystemMetadata
from src.server.search.understand import STOPWORDS, fold

logger = logging.getLogger(__name__)

MOST = 3  # other names for each first name, at most
PER_PASS = 5  # names asked about in one upkeep, at most (WAIT_SEC each: upkeep waits for them)
ANSWER_TOKENS = 1000  # room for a model that thinks first
WAIT_SEC = 20.0  # upkeep's, not a search's: it can wait a little

PROMPT = ("In someone's photos, a person is named {name!r}. For each first name in it, list up to {most} other names "
          "a person with that first name is most commonly called: its usual short forms and nicknames, and the full "
          "name it's short for if it is one (Bob: Robert). Leave out last names, titles, and words that aren't a "
          "first name (Mom, Grandma, Uncle). Respond only with JSON: {{\"names\": {{\"the first name as written\": "
          "[\"...\"]}}}}, or {{\"names\": {{}}}} when there's no first name in it.")

_PREFIX = "nicknames:"
_JSON = re.compile(r"\{.*\}", re.S)
_NAME = re.compile(r"[^\W\d_]+(?:['-][^\W\d_]+)?")  # one name: letters, maybe joined by ' or -
_WORD = re.compile(r"[^\W_]+")


def key(name: str) -> str:
    """Where a person's name's other names are kept: by the name, folded, so people by one name share it."""
    return _PREFIX + " ".join(_WORD.findall(fold(name)))


def said(reply: str, name: str) -> dict[str, list[str]]:
    """The other names a machine's reply gives for each first name in this name ({word: names}, folded): each word
    of the name, each other name one name of letters, two letters or more, never the word itself, a word of the
    name, or a word that only joins others, each once, MOST at most; a word with none left out. ValueError when the
    reply isn't the JSON asked for."""
    m = _JSON.search(reply)
    if m is None:
        raise ValueError("no JSON in the reply")
    names = json.loads(m.group(0)).get("names")
    if not isinstance(names, dict):
        raise ValueError("no names in the reply")
    words = _WORD.findall(fold(name))
    out: dict[str, list[str]] = {}
    for first, others in names.items():
        word = fold(first).strip() if isinstance(first, str) else ""
        if word not in words or not isinstance(others, list):
            continue
        kept: list[str] = out.get(word, [])
        for other in others:
            other = fold(other).strip() if isinstance(other, str) else ""
            if (_NAME.fullmatch(other) and len(other) >= 2 and other not in words and other not in STOPWORDS
                    and other not in kept):
                kept.append(other)
        if kept:
            out[word] = kept[:MOST]
    return out


def kept(session: Session) -> dict[str, dict[str, tuple[str, ...]]]:
    """Every name asked about, by its key, with what was said ({word: names})."""
    out: dict[str, dict[str, tuple[str, ...]]] = {}
    for k, value in session.execute(text("SELECT key, value FROM system_metadata WHERE key LIKE :p"),
                                    {"p": _PREFIX + "%"}):
        try:
            names = json.loads(value).get("names")
        except (ValueError, AttributeError):
            continue
        if isinstance(names, dict):
            out[k] = {w: tuple(o for o in others if isinstance(o, str)) for w, others in names.items()
                      if isinstance(w, str) and isinstance(others, list)}
    return out


def unasked(session: Session) -> list[str]:
    """The names of people (named, not dismissed) not asked about yet, each once, oldest named first."""
    asked = set(kept(session))
    out: list[str] = []
    for (name,) in session.execute(text("SELECT display_name FROM people WHERE display_name IS NOT NULL"
                                        " AND btrim(display_name) <> '' AND NOT dismissed ORDER BY created_at")):
        k = key(name)
        if k != _PREFIX and k not in asked and name not in out:
            asked.add(k)
            out.append(name)
    return out


def keep(session: Session, name: str, names: dict[str, list[str]], model: str) -> None:
    k = key(name)
    value = json.dumps({"names": names, "model": model})
    row = session.get(SystemMetadata, k)
    if row is None:
        session.add(SystemMetadata(key=k, value=value))
    else:
        row.value = value
        session.add(row)


def ask_about(session: Session, model: str, ask: Callable[[str], str]) -> dict[str, int]:
    """Ask the machine about names not asked about yet, PER_PASS at most, and keep what it said. Committed."""
    asked = failed = 0
    for name in unasked(session)[:PER_PASS]:
        try:
            reply = ask(PROMPT.format(name=name, most=MOST))
        except Exception as e:  # noqa: BLE001 — the machine's trouble: asked again at the next upkeep
            logger.info("A person's other names: no answer for %r: %s", name, e)
            failed += 1
            continue
        try:
            names = said(reply, name)
        except ValueError as e:
            logger.warning("A person's other names: the reply for %r wasn't the JSON asked for (%s): %s",
                           name, e, reply[:300])
            names = {}
        keep(session, name, names, model)
        session.commit()
        asked += 1
    return {"asked": asked, "failed": failed}
