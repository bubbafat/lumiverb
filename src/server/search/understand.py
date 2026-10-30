"""What a search asks for, understood: who, where and what kind, from the words typed (Robert, Oct 28), so
"a video of Sue dancing at California Adventure" finds the video where Susan Horvick is recognised, taken
within 5 km of California Adventure (its GPS nearer Disneyland), described as "a woman dancing".

- Who: words naming people the searcher can see, by their names' words (a first name, a last name, both
  in order), a name that's one name with theirs (nicknames.py: Sue is Susan), a family ("the Tuttles").
- Where: words naming a place a plug-in finds (Set location's places, src/server/plugin_lookups.py) with
  some of the account's clips within NEAR_KM of it: a place nobody's been to is no place to them, so
  "beach" stays a word though there's a Beach in North Dakota. Of the places by that name, the MOST_PLACES
  with the most clips around them (Robert, Oct 29: "portland" is every Portland you've been, Oregon's,
  Maine's and England's). A clip is where it's shown to be, a location guess applied too.
- What kind: video(s), photo(s) and the like, with someone or somewhere or something to look for (the
  kind alone is the media filter's).

A person or a place matches a clip by who's recognised in it or where it was, or by its words as a search
always did (a note saying "Sue", a path through "Disneyland"): understood, a search finds what it found
before, and more. Every part must match, the kind too; the words left rank what matches, as a search does.
Quoted text is never understood, nor a public page's search (it has no people). A search with nothing
understood runs as it always did (src/server/api/routers/query.py _run_text_search).
"""

from __future__ import annotations

import math
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
PLACES_LOOKED_AT = 20  # of the places by one name, those whose clips around them are counted
MOST_PLACES = 3  # of those, the ones meant: with the most of the account's clips around them (Robert, Oct 29)

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
    # Other names for words of their name, as the account's AI machine said (person_nicknames.py): (word, names).
    others: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def words(self) -> tuple[str, ...]:
        return tuple(_WORD.findall(fold(self.name)))

    def names_for(self, word: str) -> frozenset[str]:
        """Every name that's one name with a word of theirs (folded), itself included: the built-in list's and
        their own."""
        return nicknames.same_as(word) | frozenset(n for w, names in self.others if w == word for n in names)


@dataclass(frozen=True)
class Place:
    name: str
    lat: float
    lon: float
    where: str = ""  # its region, else its country: what tells it from others by its name ("Oregon")

    @property
    def shown(self) -> str:
        return f"{self.name}, {self.where}" if self.where else self.name


@dataclass(frozen=True)
class Part:
    """One thing a search asks for: who (person_ids), where (places: one, or several by one name) or what
    kind (media), with the words it was understood from (they match a clip by its words too) and what they
    were taken for."""

    kind: str
    words: str
    label: str
    person_ids: tuple[str, ...] = ()
    own_ids: tuple[str, ...] = ()  # of person_ids, those named by their own name, not another of theirs
    places: tuple[Place, ...] = ()
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


def _one_name(typed: str, word: str, person: Person) -> bool:
    return typed in person.names_for(word) or word in nicknames.same_as(typed)


def _family(typed: str, last: str) -> bool:
    return typed in (last + "s", last + "es")


def _who(people: Sequence[Person], typed: str, then: str | None) -> tuple[list[Person], bool]:
    """The people these words name: (people, whether both words named them, a first name then a last)."""
    if then is not None and then not in STOPWORDS:
        both = [p for p in people if any(_one_name(typed, w, p) and nxt in (then,) for w, nxt in zip(p.words, p.words[1:], strict=False))]
        if both:
            return both, True
    one = [p for p in people if any(_one_name(typed, w, p) for w in p.words) or (p.words and _family(typed, p.words[-1]))]
    return one, False


def _either(said: list[str]) -> str:
    return said[0] if len(said) == 1 else ", ".join(said[:-1]) + " or " + said[-1]


def _label(people: list[Person]) -> str:
    return _either(sorted({p.name for p in people}))


def places_label(places: Sequence[Place]) -> str:
    """What places by one name were taken for: "California Adventure"; "Portland (Oregon, Maine or England)"."""
    wheres = [p.where for p in places]
    if len(places) == 1:
        return places[0].name
    if len({p.name for p in places}) == 1 and all(wheres) and len(set(wheres)) == len(wheres):
        return f"{places[0].name} ({_either(wheres)})"
    return _either([p.shown for p in places])


def understand(raw: str, people: Sequence[Person], find_place: Callable[[str], Sequence[Place]]) -> Understood:
    """What a search's words ask for. people: those the searcher can see, by name; find_place: the places a
    phrase names that the account's clips are around, those meant first (none: it names no place)."""
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
            # Their names as typed, and the others of the words of theirs it named.
            theirs = {n for p in named for w in p.words if any(_one_name(t, w, p) for t in said) for n in p.names_for(w)}
            also = sorted(set().union(theirs, *(nicknames.same_as(w) for w in said)) - set(said))
            own = {p.person_id for p in named if said[0] in p.words}
            parts.append((i, Part(PERSON, " ".join(said), _label(named),
                                  person_ids=tuple(sorted({p.person_id for p in named})),
                                  own_ids=tuple(sorted(own)), also=tuple(also))))
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
        places = tuple(find_place(phrase))
        if places:
            for k in range(s, e + 1):
                taken[k] = True
            if usable(s - 1) and word[s - 1] in BEFORE_A_PLACE:
                taken[s - 1] = True  # "at": it said where, and it's said
            parts.append((s, Part(PLACE, phrase, places_label(places), places=places)))
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


def _near_sql(places: Sequence[Place], params: dict) -> list[str]:
    """SQL on active_assets a (LOCATION_JOIN as loc), one for each place: the clip is within NEAR_KM of it,
    where it's shown to be (a location guess applied counts)."""
    from src.server.models.query_filter import INCLUDE_GUESSES_PARAM, NearLocation

    params[INCLUDE_GUESSES_PARAM] = True
    counter = [0]
    return [NearLocation(lat=p.lat, lon=p.lon, radius_km=NEAR_KM).to_sql(params, counter) for p in places]


def clips_near(session: Session, places: Sequence[Place], library_ids: list[str] | None) -> dict[str, Place]:
    """The searcher's clips within NEAR_KM of any of these places, each with the first of them it's near."""
    from src.server.models.query_filter import LOCATION_JOIN

    if not places:
        return {}
    params: dict = {}
    near = _near_sql(places, params)
    which = "CASE " + " ".join(f"WHEN {n} THEN {i}" for i, n in enumerate(near)) + " END"
    rows = session.execute(text(
        f"SELECT a.asset_id, {which} FROM active_assets a {LOCATION_JOIN}"
        f" WHERE ({' OR '.join(near)}){_in(library_ids, params)}"), params)
    return {r[0]: places[r[1]] for r in rows}


def clips_around(session: Session, places: Sequence[Place], library_ids: list[str] | None) -> list[int]:
    """How many of the searcher's clips are within NEAR_KM of each place: one look for them all."""
    from src.server.models.query_filter import LOCATION_JOIN

    if not places:
        return []
    params: dict = {}
    near = _near_sql(places, params)
    counts = ", ".join(f"count(*) FILTER (WHERE {n})" for n in near)
    row = session.execute(text(
        f"SELECT {counts} FROM active_assets a {LOCATION_JOIN}"
        f" WHERE ({' OR '.join(near)}){_in(library_ids, params)}"), params).one()
    return [int(n) for n in row]


def _km(a: Place, b: Place) -> float:
    """How far apart two places are, over the earth (haversine)."""
    la, lb = math.radians(a.lat), math.radians(b.lat)
    h = math.sin((lb - la) / 2) ** 2 + math.cos(la) * math.cos(lb) * math.sin(math.radians(b.lon - a.lon) / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def _where(found: dict) -> str:
    """What tells a place a plug-in found from others by its name: its region, else its country."""
    region, country = found.get("region") or "", found.get("country") or ""
    return region if region and region != found["name"] else country


def place_finder(session: Session, library_ids: list[str] | None) -> Callable[[str], list[Place]]:
    """The places a phrase names, as a plug-in that finds places names them (their own name, or the name that
    matched), that the searcher means by it: those with some of their clips within NEAR_KM, the most first (ties
    in the plug-in's order), MOST_PLACES at most. Two whose clips around them could be the same (within twice
    NEAR_KM of each other) are one, the first. None when none has any clips around it."""
    from src.server.plugin_lookups import find_places

    def find(phrase: str) -> list[Place]:
        found, _credits, _unavailable = find_places(session, phrase, PLACES_LOOKED_AT)
        named = [Place(p["name"], p["lat"], p["lon"], _where(p)) for p in found
                 if phrase in (fold(p["name"]), fold(p.get("matched") or ""))]
        counted = sorted(zip(clips_around(session, named, library_ids), named, strict=True), key=lambda c: -c[0])
        meant: list[Place] = []
        for around, place in counted:
            if around and len(meant) < MOST_PLACES and all(_km(place, m) > 2 * NEAR_KM for m in meant):
                meant.append(place)
        return meant

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
    from src.server.search import person_nicknames

    said = person_nicknames.kept(session)
    people = [Person(p.person_id, p.name, tuple(sorted(said.get(person_nicknames.key(p.name), {}).items())))
              for p in people_in_sight(session, library_ids)]
    return understand(raw, people, place_finder(session, library_ids))
