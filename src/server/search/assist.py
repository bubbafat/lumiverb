"""AI-assisted search (Robert, Oct 28: a local search, free, and an assisted one, so people control their costs): the
words of a search that name a kind of thing are searched with the particular things of that kind too, as the account's
AI machine lists them, so "snacks" finds a scene described as "a basket of tortilla chips", and "disney theme park" one
of "a roller coaster". Asked only when the search says so (`assist:yes`) and the account lets searches ask (Settings ›
AI, off until an admin turns it on), of the machines describing its photos, with their model.

A search's words are asked about together, one question (Robert, Oct 29: the whole phrase, so "theme park" is a kind
where "theme" and "park" alone aren't), once per model: what the machine said is kept in the account's database
(system_metadata), nothing for words naming no kind. A search's results and its filters' counts, asked for at once, ask
once: the second waits for the first's answer (Robert, Oct 29: minimize costs). A machine that's off or busy leaves
the search its words, and nothing is kept, so a later search asks again. Quoted words and a file's name are never asked
about.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, wait

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlmodel import Session

from src.server.models.tenant import SystemMetadata
from src.server.search.query_builder import pieces
from src.server.search.understand import STOPWORDS, fold

logger = logging.getLogger(__name__)

MOST_ASKED = 4  # searches combined asked about in one request, at most (the others are searched as they are)
MOST_ALSO = 12  # things of a kind, at most
MOST_CHARS = 200  # the words asked about, at most (a longer search is searched as it is)
WAIT_SEC = 10.0  # how long a search waits for the machine's answers, at most
ANSWER_TOKENS = 600  # a few kinds, MOST_ALSO things each
LOCK_NOT_AVAILABLE = "55P03"  # PostgreSQL's lock_timeout

PROMPT = ("Someone searching the descriptions of their photos and videos typed: {words!r}. Which of its words name a "
          "kind of thing: one word, or a few in a row that name one together? Only the words that name the kind: in "
          "'snacks theme park sunset', 'snacks' and 'theme park' are kinds, and 'sunset' isn't, nor are all the words "
          "together; in 'red car', 'car' is the kind, not 'red'. For each kind, list up to {most} particular things "
          "of that kind a description might name instead, one or two words each, the ones most often seen in "
          "everyday photos and videos, at home or on a trip. Each must be of that kind (for snacks, popcorn but not "
          "pizza) and never use the kind's own words. Leave out words that name a particular thing, or no kind of "
          "thing. Respond only with JSON: {{\"kinds\": {{\"the kind's words as typed\": [\"...\"]}}}}, or "
          "{{\"kinds\": {{}}}} when none do.")

_KEY = "search_also:{model}:{words}"
_JSON = re.compile(r"\{.*\}", re.S)
_THING = re.compile(r"[^\W\d_][^\W_]*(?:[ '-][^\W_]+)?")  # one or two words of letters


def asked_about(raw: str) -> str:
    """The words of a search asked about, together: those outside quotes, not a file's name (words joined) nor a word
    that only joins others, lowercase and accents aside, in order; "" when none could name a kind of thing (three
    letters or more, not a number) or they're longer than MOST_CHARS."""
    words = [fold(p.words[0]) for p in pieces(raw) if not p.quoted and len(p.words) == 1]
    words = [w for w in words if w not in STOPWORDS]
    asked = " ".join(words)
    if len(asked) > MOST_CHARS or not any(len(w) >= 3 and not w.isdigit() for w in words):
        return ""
    return asked


def _things(listed: list, kind: list[str]) -> list[str]:
    out: list[str] = []
    for thing in listed:
        if not isinstance(thing, str):
            continue
        thing = " ".join(fold(thing).split())
        if _THING.fullmatch(thing) and not set(thing.split()) & set(kind) and thing not in out:
            out.append(thing)
    return out[:MOST_ALSO]


def said(reply: str, words: str) -> dict[str, list[str]]:
    """The kinds a machine's reply finds in the words asked about, each with its things, in the words' order: a kind is
    some of the words in a row (lowercase, accents aside), the longer kept where two overlap; its things lowercase, one
    or two words of letters each, never sharing a word with it, each once, MOST_ALSO at most; a kind with none left
    out. ValueError when the reply isn't the JSON asked for."""
    m = _JSON.search(reply)
    if m is None:
        raise ValueError("no JSON in the reply")
    kinds = json.loads(m.group(0)).get("kinds")
    if not isinstance(kinds, dict):
        raise ValueError("no kinds in the reply")
    asked = words.split()
    found: list[tuple[int, int, str, list[str]]] = []  # (where, how many words, the kind, its things)
    for kind, listed in kinds.items():
        its = fold(kind).split() if isinstance(kind, str) else []
        at = next((i for i in range(len(asked) - len(its) + 1) if its and asked[i:i + len(its)] == its), None)
        things = _things(listed, its) if at is not None and isinstance(listed, list) else []
        if things:
            found.append((at, len(its), " ".join(its), things))  # type: ignore[arg-type]
    taken: set[int] = set()
    kept_: list[tuple[int, str, list[str]]] = []
    for at, n, kind, things in sorted(found, key=lambda f: -f[1]):  # the longer first
        if taken.isdisjoint(range(at, at + n)):
            taken.update(range(at, at + n))
            kept_.append((at, kind, things))
    return {kind: things for _, kind, things in sorted(kept_)}


def by_word(kinds: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """The things of each kind as text_search.build takes them, by word: a kind's things match as its last word does,
    the word a phrase names its thing by ("theme park": park, or a roller coaster; "red car": car, or a truck, which
    isn't red). A machine may take a word saying which (red) for part of the kind: it's still asked for."""
    out: dict[str, list[str]] = {}
    for kind, things in kinds.items():
        last = kind.split()[-1]
        out[last] = list(dict.fromkeys([*out.get(last, []), *things]))
    return out


def kept(session: Session, model: str, words: str) -> dict[str, list[str]] | None:
    """What was said of these words with this model ({kind: things}), or None when they haven't been asked about (a
    word asked about alone, as before Oct 29, is asked again)."""
    row = session.get(SystemMetadata, _KEY.format(model=model, words=words))
    if row is None:
        return None
    try:
        value = json.loads(row.value)
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    return {kind: [t for t in things if isinstance(t, str)] for kind, things in value.items()
            if isinstance(kind, str) and isinstance(things, list)}


def keep(session: Session, model: str, words: str, kinds: dict[str, list[str]]) -> None:
    key = _KEY.format(model=model, words=words)
    row = session.get(SystemMetadata, key)
    if row is None:
        session.add(SystemMetadata(key=key, value=json.dumps(kinds)))
    else:
        row.value = json.dumps(kinds)
        session.add(row)


def _asking(session: Session, key: str) -> bool:
    """Whether this request asks about these words: no other is asking now. Held until this request commits."""
    return bool(session.execute(text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"), {"k": key}).scalar())


def _waited(session: Session, key: str, seconds: float) -> bool:
    """Waits for the request asking about these words to keep what was said (or give up), seconds at most. Its lock
    shared: those waiting never wait for each other, and one asking waits for nothing while it holds its own."""
    try:
        with session.begin_nested():
            session.execute(text("SELECT set_config('lock_timeout', :t, true)"),
                            {"t": f"{max(1, int(seconds * 1000))}ms"})
            session.execute(text("SELECT pg_advisory_xact_lock_shared(hashtext(:k))"), {"k": key})
        return True
    except OperationalError as e:
        if getattr(e.orig, "pgcode", None) != LOCK_NOT_AVAILABLE:
            raise
        return False


def kinds(session: Session, asked: Sequence[str], model: str,
          ask: Callable[[str], str] | None) -> dict[str, dict[str, list[str]]]:
    """The kinds each search's words name, with their things ({words: {kind: things}}, only words with some): kept
    ones, and those the machine is asked about now (ask: the prompt to its reply; None when no machine can be asked),
    MOST_ASKED at most, within WAIT_SEC. Words another request is asking about now are waited for, not asked again.
    Words the machine couldn't answer are left as they are, and nothing kept of them."""
    out: dict[str, dict[str, list[str]]] = {}
    unknown: list[str] = []
    for words in asked:
        found = kept(session, model, words)
        if found is None:
            unknown.append(words)
        elif found:
            out[words] = found
    unknown = unknown[:MOST_ASKED]
    if not unknown or ask is None:
        return out

    started = time.monotonic()
    mine: list[str] = []
    theirs: list[str] = []
    for words in unknown:
        if not _asking(session, _KEY.format(model=model, words=words)):
            theirs.append(words)
        elif (found := kept(session, model, words)) is None:
            mine.append(words)
        elif found:  # asked about and kept since they were looked for
            out[words] = found

    def one(words: str) -> dict[str, list[str]]:
        return said(ask(PROMPT.format(words=words, most=MOST_ALSO)), words)

    if mine:
        with ThreadPoolExecutor(max_workers=len(mine)) as pool:  # MOST_ASKED at most: bounded
            futures = {pool.submit(one, w): w for w in mine}
            done, _ = wait(futures, timeout=WAIT_SEC)
        for future in done:
            words = futures[future]
            try:
                found = future.result()
            except Exception as e:  # noqa: BLE001 — the machine's trouble: the words are searched as they are
                logger.info("AI-assisted search: no answer for %r: %s", words, e)
                continue
            keep(session, model, words, found)
            if found:
                out[words] = found
    session.commit()  # what was said kept, and those waiting for it let go
    for words in theirs:
        left = WAIT_SEC - (time.monotonic() - started)
        if left > 0 and _waited(session, _KEY.format(model=model, words=words), left) \
                and (found := kept(session, model, words)):
            out[words] = found
    if theirs:
        session.commit()
    logger.info("AI-assisted search: asked about %d, waited for %d, in %.1fs", len(mine), len(theirs),
                time.monotonic() - started)
    return out


def asker(tenant_id: str, *, max_tokens: int = ANSWER_TOKENS,
          timeout: float = WAIT_SEC) -> tuple[str, Callable[[str], str] | None]:
    """The account's model for descriptions and a way to ask its machines a question (each describing machine that's
    on, in order, until one answers), or (model, None) when there's none to ask."""
    from src.processing.workers.captions.openai_caption import OpenAICompatibleCaptionProvider
    from src.server.database import get_control_session
    from src.server.models.control_plane import Tenant
    from src.server.repository.ai_machines import job_model, key_of, machines
    from src.shared.vision_endpoint import VisionEndpointError, allowed

    with get_control_session() as ctrl:
        model = job_model(ctrl.get(Tenant, tenant_id), "vision")
        doing = [m for m in machines(ctrl, tenant_id)
                 if m.enabled and not m.built_in and "vision" in m.jobs and m.online is not False]
        for m in doing:
            ctrl.expunge(m)
    if not model or not doing:
        return model, None

    def ask(prompt: str) -> str:
        last: Exception | None = None
        for m in doing:
            try:
                allowed(m.api_url)
                provider = OpenAICompatibleCaptionProvider(m.api_url, model, key_of(m) or None,
                                                           fingerprint=m.fingerprint)
                return provider.answer(prompt, max_tokens=max_tokens, timeout=timeout)
            except (VisionEndpointError, OSError, ValueError) as e:  # requests' errors are OSErrors: the next one
                last = e
        raise last or RuntimeError("no machine answered")

    return model, ask
