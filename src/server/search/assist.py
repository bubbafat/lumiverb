"""AI-assisted search (Robert, Oct 28: a local search, free, and an assisted one, so people control their costs): a
search's words that name a kind of thing are searched with the particular things of that kind too, as the account's
AI machine lists them, so "snacks" finds a scene described as "a basket of tortilla chips". Asked only when the search
says so (`assist:yes`) and the account lets searches ask (Settings › AI, off until an admin turns it on), of the
machines describing its photos, with their model; each word once per model, kept in the account's database
(system_metadata) with what it said (nothing, for a word that's already particular). A machine that's off or busy
leaves the search its words, and nothing is kept, so a later search asks again. Quoted words are never asked about.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, wait

from sqlmodel import Session

from src.server.models.tenant import SystemMetadata
from src.server.search.query_builder import pieces
from src.server.search.understand import STOPWORDS, fold

logger = logging.getLogger(__name__)

MOST_ASKED = 4  # words asked about in one search, at most (the others are searched as they are)
MOST_ALSO = 12  # things of a kind, at most, for a word
WAIT_SEC = 10.0  # how long a search waits for the machine's answers, at most
ANSWER_TOKENS = 200

PROMPT = ("Someone searching the descriptions of their photos and videos typed: {word!r}. If that names a kind of "
          "thing, list up to {most} particular things of that kind a description might name instead, one or two words "
          "each, the ones most often seen in everyday photos and videos, at home or on a trip. Each must be of that "
          "kind: for snacks, popcorn but not pizza. Leave out the word itself and phrases with it. If it's already a "
          "particular thing, or not a kind of thing, list nothing. Respond only with JSON: {{\"also\": [\"...\"]}}")

_KEY = "search_also:{model}:{word}"
_JSON = re.compile(r"\{.*\}", re.S)
_THING = re.compile(r"[^\W\d_][^\W_]*(?:[ '-][^\W_]+)?")  # one or two words of letters


def asked_about(raw: str) -> list[str]:
    """The words of a search that could name a kind of thing (outside quotes, not joining words, three letters or
    more, not a number), in order, each once."""
    out: list[str] = []
    for p in pieces(raw):
        w = fold(p.words[0])
        if not p.quoted and w not in STOPWORDS and len(w) >= 3 and not w.isdigit() and w not in out:
            out.append(w)
    return out


def said(reply: str, word: str) -> list[str]:
    """The things a machine's reply lists for a word: lowercase, one or two words of letters each, never the word or
    a phrase with it, each once, MOST_ALSO at most. ValueError when the reply isn't the JSON asked for."""
    m = _JSON.search(reply)
    if m is None:
        raise ValueError("no JSON in the reply")
    listed = json.loads(m.group(0)).get("also")
    if not isinstance(listed, list):
        raise ValueError("no list in the reply")
    out: list[str] = []
    for thing in listed:
        if not isinstance(thing, str):
            continue
        thing = " ".join(fold(thing).split())
        if _THING.fullmatch(thing) and word not in thing.split() and thing != word and thing not in out:
            out.append(thing)
    return out[:MOST_ALSO]


def kept(session: Session, model: str, word: str) -> list[str] | None:
    """What was said of a word with this model, or None when it hasn't been asked."""
    row = session.get(SystemMetadata, _KEY.format(model=model, word=word))
    if row is None:
        return None
    try:
        value = json.loads(row.value)
    except ValueError:
        return None
    return [t for t in value if isinstance(t, str)] if isinstance(value, list) else None


def keep(session: Session, model: str, word: str, things: list[str]) -> None:
    key = _KEY.format(model=model, word=word)
    row = session.get(SystemMetadata, key)
    if row is None:
        session.add(SystemMetadata(key=key, value=json.dumps(things)))
    else:
        row.value = json.dumps(things)
        session.add(row)


def also(session: Session, words: Sequence[str], model: str,
         ask: Callable[[str], str] | None) -> dict[str, list[str]]:
    """The things of the kinds these words name ({word: things}, only words with some): kept ones, and those the
    machine is asked about now (ask: the word's prompt to its reply; None when no machine can be asked), MOST_ASKED
    at most, within WAIT_SEC. A word the machine couldn't answer is left out, and nothing kept of it."""
    out: dict[str, list[str]] = {}
    unknown: list[str] = []
    for word in words:
        things = kept(session, model, word)
        if things is None:
            unknown.append(word)
        elif things:
            out[word] = things
    unknown = unknown[:MOST_ASKED]
    if not unknown or ask is None:
        return out

    def one(word: str) -> list[str]:
        return said(ask(PROMPT.format(word=word, most=MOST_ALSO)), word)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(unknown)) as pool:  # MOST_ASKED at most: bounded
        futures = {pool.submit(one, w): w for w in unknown}
        done, _ = wait(futures, timeout=WAIT_SEC)
    for future in done:
        word = futures[future]
        try:
            things = future.result()
        except Exception as e:  # noqa: BLE001 — the machine's trouble: the word is searched as it is
            logger.info("AI-assisted search: no answer for %r: %s", word, e)
            continue
        keep(session, model, word, things)
        if things:
            out[word] = things
    if done:
        session.commit()
    logger.info("AI-assisted search: asked about %d words in %.1fs", len(unknown), time.monotonic() - started)
    return out


def asker(tenant_id: str) -> tuple[str, Callable[[str], str] | None]:
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
                return provider.answer(prompt, max_tokens=ANSWER_TOKENS, timeout=WAIT_SEC)
            except (VisionEndpointError, OSError, ValueError) as e:  # requests' errors are OSErrors: the next one
                last = e
        raise last or RuntimeError("no machine answered")

    return model, ask
