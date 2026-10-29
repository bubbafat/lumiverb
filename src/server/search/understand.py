"""What a search asks for, understood: who, where and what kind, from the words typed (Robert, Oct 28), so
"a video of Sue dancing at California Adventure" finds the video where Susan Horvick is recognised, taken
within 5 km of California Adventure (its GPS nearer Disneyland), described as "a woman dancing".

- Who: words naming people the searcher can see, by their names' words (a first name, a last name, both
  in order), a name that's one name with theirs (nicknames.py: Sue is Susan), a family ("the Tuttles").
- Where: words naming a place a plug-in finds (Set location's places, src/server/plugin_lookups.py) with
  some of the account's clips within NEAR_KM of it: a place nobody's been to is no place to them, so
  "beach" stays a word though there's a Beach in North Dakota. A clip is where it's shown to be, a
  location guess applied too.
- What kind: video(s), photo(s) and the like, with someone or somewhere or something to look for (the
  kind alone is the media filter's).

A person or a place matches a clip by who's recognised in it or where it was, or by its words as a search
always did (a note saying "Sue", a path through "Disneyland"): understood, a search finds what it found
before, and more. Every part must match, the kind too; the words left rank what matches, as a search does.
Quoted text is never understood, nor a public page's search (it has no people). A search with nothing
understood runs as it always did (src/server/api/routers/query.py _run_text_search).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlmodel import Session

from src.server.search import nicknames
from src.server.search.query_builder import pieces

NEAR_KM = 5.0  # "at" a place: within 5 km of it (Robert, Oct 28)
MOST_PLACE_WORDS = 4  # a place's name, at most ("Disney's Grand Californian Hotel")
MOST_LOOKUPS = 8  # names asked of the places plug-ins in one search, at most
PLACES_LOOKED_AT = 5  # of the places by one name, the one with the account's clips around it is meant

PERSON, PLACE, KIND = "person", "place", "kind"

# What a kind's words ask for (assets.media_type).
KINDS = {**dict.fromkeys(("video", "videos", "clip", "clips", "movie", "movies", "footage"), "video"),
         **dict.fromkeys(("photo", "photos", "picture", "pictures", "pic", "pics", "image", "images"), "image")}
KIND_LABELS = {"video": "videos", "image": "photos"}

# Words that only join others (English's stop words, as the text search drops them): never a name, nor where
# a place's name starts or ends.
_STOPWORDS = """
i me my myself we our ours ourselves you your yours yourself yourselves he him his himself she her hers herself
it its itself they them their theirs themselves what which who whom this that these those am is are was were be
been being have has had having do does did doing a an the and but if or because as until while of at by for with
about against between into through during before after above below to from up down in out on off over under again
further then once here there when where why how all any both each few more most other some such no nor not only
own same so than too very s t can will just don should now near around
"""
STOPWORDS = frozenset(_STOPWORDS.split())
# Words before a place: a phrase after one is asked about first.
BEFORE_A_PLACE = frozenset(("at", "in", "near", "around", "from", "to", "by"))

_WORD = re.compile(r"[^\W_]+")


def fold(text_: str) -> str:
    """Lowercase, accents aside: how names are compared ("José" is "jose")."""
    return "".join(c for c in unicodedata.normalize("NFKD", text_.lower()) if not unicodedata.combining(c))


@dataclass(frozen=True)
class Person:
    person_id: str
    name: str

    @property
    def words(self) -> tuple[str, ...]:
        return tuple(_WORD.findall(fold(self.name)))


@dataclass(frozen=True)
class Place:
    name: str
    lat: float
    lon: float


@dataclass(frozen=True)
class Part:
    """One thing a search asks for: who (person_ids), where (place) or what kind (media), with the words
    it was understood from (they match a clip by its words too) and what they were taken for."""

    kind: str
    words: str
    label: str
    person_ids: tuple[str, ...] = ()
    place: Place | None = None
    media: str | None = None
    also: tuple[str, ...] = ()  # the words it matches a clip by too: a name's others ("susan" is "sue" too)

    @property
    def searched(self) -> str:
        """The words it matches a clip's words by: a person's any of their names, a place's name whole (its
        words in a row: "California Adventure", never "adventure" alone)."""
        if self.kind == PLACE and " " in self.words:
            return f'"{self.words}"'
        return " ".join((self.words, *self.also))


@dataclass(frozen=True)
class Understood:
    parts: tuple[Part, ...]
    rest: str  # the words left, searched as a search always was ("" for none)

    def said(self) -> list[dict]:
        """What it was understood as, in order (GET /v1/query's `understood`)."""
        out = [{"kind": p.kind, "words": p.words, "label": p.label} for p in self.parts]
        return out + ([{"kind": "words", "words": self.rest, "label": self.rest}] if self.rest else [])


def _one_name(typed: str, word: str) -> bool:
    return word in nicknames.same_as(typed)


def _family(typed: str, last: str) -> bool:
    return typed in (last + "s", last + "es")


def _who(people: Sequence[Person], typed: str, then: str | None) -> tuple[list[Person], bool]:
    """The people these words name: (people, whether both words named them, a first name then a last)."""
    if then is not None and then not in STOPWORDS:
        both = [p for p in people if any(_one_name(typed, w) and nxt in (then,) for w, nxt in zip(p.words, p.words[1:], strict=False))]
        if both:
            return both, True
    one = [p for p in people if any(_one_name(typed, w) for w in p.words) or (p.words and _family(typed, p.words[-1]))]
    return one, False


def _label(people: list[Person]) -> str:
    names = sorted({p.name for p in people})
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def understand(raw: str, people: Sequence[Person], find_place: Callable[[str], Place | None]) -> Understood:
    """What a search's words ask for. people: those the searcher can see, by name; find_place: the place a
    phrase names that the account's clips are around, or None."""
    typed = pieces(raw)
    free = [not p.quoted and len(p.words) == 1 for p in typed]
    word = [fold(p.words[0]) if f else "" for p, f in zip(typed, free, strict=True)]
    taken = [False] * len(typed)
    parts: list[tuple[int, Part]] = []

    def usable(i: int) -> bool:
        return 0 <= i < len(typed) and free[i] and not taken[i]

    # Who
    i = 0
    while i < len(typed):
        if not usable(i) or word[i] in STOPWORDS or len(word[i]) < 2:
            i += 1
            continue
        then = word[i + 1] if usable(i + 1) else None
        named, both = _who(people, word[i], then)
        if named:
            span = (i, i + 1) if both else (i,)
            for k in span:
                taken[k] = True
            said = [word[k] for k in span]
            also = sorted(set().union(*(nicknames.same_as(w) for w in said)) - set(said))
            parts.append((i, Part(PERSON, " ".join(said), _label(named),
                                  person_ids=tuple(sorted({p.person_id for p in named})), also=tuple(also))))
            i += len(span)
            continue
        i += 1

    # What kind: one kind asked for (both is neither)
    asked = {KINDS[word[i]] for i in range(len(typed)) if usable(i) and word[i] in KINDS}
    if len(asked) == 1:
        (media,) = asked
        at = [i for i in range(len(typed)) if usable(i) and word[i] in KINDS]
        for i in at:
            taken[i] = True
        parts.append((at[0], Part(KIND, " ".join(word[i] for i in at), KIND_LABELS[media], media=media)))

    # Where: the phrases left, those after "at", "in"... first, then the longest
    spans: list[tuple[bool, int, int]] = []
    for s in range(len(typed)):
        for e in range(s, min(s + MOST_PLACE_WORDS, len(typed))):
            if not all(usable(k) for k in range(s, e + 1)):
                break
            if word[s] not in STOPWORDS and word[e] not in STOPWORDS:
                spans.append((not (usable(s - 1) and word[s - 1] in BEFORE_A_PLACE), -(e - s), s))
    for _, minus_length, s in sorted(spans)[:MOST_LOOKUPS]:
        e = s - minus_length
        phrase = " ".join(word[s:e + 1])
        place = find_place(phrase)
        if place is not None:
            for k in range(s, e + 1):
                taken[k] = True
            if usable(s - 1) and word[s - 1] in BEFORE_A_PLACE:
                taken[s - 1] = True  # "at": it said where, and it's said
            parts.append((s, Part(PLACE, phrase, place.name, place=place)))
            break

    # The words that join others go: the search reads past them anyway ("a video of ... dancing" is "dancing").
    rest = " ".join(p.said for i, p in enumerate(typed) if not taken[i] and (p.quoted or fold(p.words[0]) not in STOPWORDS))
    if not parts or not rest and all(p.kind == KIND for _, p in parts):
        # Nothing understood, or "videos" alone (every one: the media filter's): the words as typed, as before.
        return Understood((), raw)
    return Understood(tuple(p for _, p in sorted(parts, key=lambda ip: ip[0])), rest)


# ---------------------------------------------------------------------------
# What's known in the account
# ---------------------------------------------------------------------------


def _in(library_ids: list[str] | None, params: dict) -> str:
    if library_ids is None:
        return ""
    params["library_ids"] = list(library_ids)
    return " AND a.library_id = ANY(CAST(:library_ids AS text[]))"


def people_in_sight(session: Session, library_ids: list[str] | None) -> list[Person]:
    """The people with a name recognised in a clip the searcher can see."""
    params: dict = {}
    rows = session.execute(text(f"""
        SELECT p.person_id, p.display_name FROM people p
        WHERE p.display_name IS NOT NULL AND NOT p.dismissed AND EXISTS (
            SELECT 1 FROM faces f JOIN active_assets a ON a.asset_id = f.asset_id
            WHERE f.person_id = p.person_id{_in(library_ids, params)})
    """), params).all()
    return [Person(r.person_id, r.display_name) for r in rows]


def _near_sql(lat: float, lon: float, params: dict) -> str:
    """SQL on active_assets a (LOCATION_JOIN as loc): the clip is within NEAR_KM of the point, where it's
    shown to be (a location guess applied counts)."""
    from src.server.models.query_filter import INCLUDE_GUESSES_PARAM, NearLocation

    params[INCLUDE_GUESSES_PARAM] = True
    return NearLocation(lat=lat, lon=lon, radius_km=NEAR_KM).to_sql(params, [0])


def clips_near(session: Session, place: Place, library_ids: list[str] | None, *, most: int | None = None) -> list[str]:
    from src.server.models.query_filter import LOCATION_JOIN

    params: dict = {}
    where = _near_sql(place.lat, place.lon, params)
    limit = ""
    if most is not None:
        params["most"] = most
        limit = " LIMIT :most"
    return [r[0] for r in session.execute(text(
        f"SELECT a.asset_id FROM active_assets a {LOCATION_JOIN} WHERE {where}{_in(library_ids, params)}{limit}"),
        params)]


def place_finder(session: Session, library_ids: list[str] | None) -> Callable[[str], Place | None]:
    """The place a phrase names, as a plug-in that finds places names it (its own name, or the name that
    matched), with the most of the searcher's clips within NEAR_KM of it; None when none has any."""
    from src.server.plugin_lookups import find_places

    def find(phrase: str) -> Place | None:
        found, _credits, _unavailable = find_places(session, phrase, PLACES_LOOKED_AT)
        named = [p for p in found if phrase in (fold(p["name"]), fold(p.get("matched") or ""))]
        best: tuple[int, Place | None] = (0, None)
        for p in named:
            place = Place(p["name"], p["lat"], p["lon"])
            around = len(clips_near(session, place, library_ids, most=1))
            if around > best[0]:
                best = (around, place)
        return best[1]

    return find


def clips_with(session: Session, person_ids: Sequence[str], library_ids: list[str] | None) -> list[str]:
    """The clips the searcher can see where one of these people is recognised."""
    params: dict = {"people": list(person_ids)}
    return [r[0] for r in session.execute(text(f"""
        SELECT DISTINCT f.asset_id FROM faces f JOIN active_assets a ON a.asset_id = f.asset_id
        WHERE f.person_id = ANY(CAST(:people AS text[])){_in(library_ids, params)}
    """), params)]


def of_kind(session: Session, asset_ids: Sequence[str] | None, media: str,
            library_ids: list[str] | None) -> list[str]:
    """Of these clips (None: every one the searcher can see), those of this kind."""
    params: dict = {"media": media}
    among = ""
    if asset_ids is not None:
        params["ids"] = list(asset_ids)
        among = " AND a.asset_id = ANY(CAST(:ids AS text[]))"
    return [r[0] for r in session.execute(text(
        f"SELECT a.asset_id FROM active_assets a WHERE a.media_type = :media{among}{_in(library_ids, params)}"),
        params)]


def read(session: Session, raw: str, library_ids: list[str] | None) -> Understood:
    """What a signed-in search's words ask for, with what's known in the account (read only when a word
    outside quotes could ask for something)."""
    if not any(not p.quoted and fold(p.words[0]) not in STOPWORDS for p in pieces(raw)):
        return Understood((), raw)
    return understand(raw, people_in_sight(session, library_ids), place_finder(session, library_ids))
