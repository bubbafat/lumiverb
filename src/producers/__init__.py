"""The producers: one folder each, found here (ADR-016 phase 4), and those
added while Lumiverb runs (each installed plug-in's work, ADR-023).

Each subpackage of this one is a producer: its ``__init__`` sets
``PRODUCER`` to a ProducerSpec (src/producers/contract.py), and only
declares; its work is a Work in work.py (src/producers/runner.py). Everything
that lists producers, their pools or their AI jobs reads them from here.

A plug-in the server admin installs brings its work while the API and the
scheduler run, so the list can change: ``use_added`` puts in place those added,
and ``registry()`` hands out a new dict each time it changes (never one
changed in place, so a caller holding one sees one list throughout). What's
worked out from the list is worked out again when it changes
(src/producers/live.py). An added producer is a plug-in's work and nothing
else (``_check_added``): it reads no original, makes nothing at a scan and
uses no AI job, so what's worked out from those once, at import (a reader's
work, the AI jobs), stays true.
"""

from __future__ import annotations

import importlib
import pkgutil
import threading
from collections.abc import Iterable
from dataclasses import replace

from src.producers.contract import AiJob, Pool, ProducerSpec
from src.producers.names import FILES, LUMIVERB, NAME, ORIGINAL, PLUGIN_ID, needs_of, sql_spelling
from src.producers.pools import SCANS

_registry: dict[str, ProducerSpec] | None = None  # Lumiverb's own, found in this package
_added: dict[str, ProducerSpec] = {}  # added while running, by artifact
_added_by: dict[str, dict[str, ProducerSpec]] = {}  # the same, by who added them (use_added's by)
# The list handed out, and what it was made from: made again when either changes.
_listed: tuple[dict[str, ProducerSpec], dict[str, ProducerSpec], dict[str, ProducerSpec]] | None = None
_adding = threading.Lock()


def _own() -> dict[str, ProducerSpec]:
    """Lumiverb's own producers by artifact, in list order. Found on first use."""
    global _registry
    if _registry is None:
        found: list[ProducerSpec] = []
        for module in pkgutil.iter_modules(__path__):
            if module.ispkg:
                spec = getattr(importlib.import_module(f"{__name__}.{module.name}"), "PRODUCER", None)
                if not isinstance(spec, ProducerSpec):
                    # Half-loaded (it imported what imports the registry) or not a producer:
                    # left out quietly, it would never run and nothing would say why.
                    raise ValueError(f"src/producers/{module.name} declares no PRODUCER (a ProducerSpec)")
                if spec.needs:
                    raise ValueError(f"{spec.artifact}: what a producer is made from is its reads (names), never"
                                     " its needs, which are worked out from them")
                found.append(spec)
        found.sort(key=lambda p: (p.order, p.artifact))
        made_by = {p.makes.name: p.artifact for p in found if p.makes is not None}
        found = [replace(p, needs=needs_of(p.reads, made_by)) for p in found]
        _check(found)
        _check_names(found)
        _registry = {p.artifact: p for p in found}
    return _registry


def registry() -> dict[str, ProducerSpec]:
    """Every producer by its artifact, in list order: Lumiverb's own and those
    added. A new dict when that changes; never change the one handed out."""
    global _listed
    own, added = _own(), _added
    listed = _listed
    if listed is None or listed[0] is not own or listed[1] is not added:
        ordered = sorted((*own.values(), *added.values()), key=lambda p: (p.order, p.artifact))
        listed = _listed = (own, added, {p.artifact: p for p in ordered})
    return listed[2]


# Who adds producers: the installed plug-ins (src/server/plugin_producers.py).
INSTALLED = "installed"


def use_added(specs: Iterable[ProducerSpec], by: str = INSTALLED) -> bool:
    """These producers added, in place of those this one added before (by:
    who adds them; the installed plug-ins, or a test): the list changes only
    when they differ. Each is checked with Lumiverb's own and the others
    added (ValueError, and the list stays as it was). Whether it changed."""
    global _added, _added_by
    with _adding:
        own = _own()
        made_by = {p.makes.name: p.artifact for p in own.values() if p.makes is not None}
        specs = list(specs)
        if any(p.needs for p in specs):
            raise ValueError("An added producer's needs are worked out from its reads, never declared")
        # A plug-in is handed its other inputs as the clip has them now (its inputs' hash): it waits only for
        # the files it's handed (a photo's preview), as before names.
        mine = {p.artifact: replace(p, needs=needs_of([n for n in p.reads if n in FILES], made_by)) for p in specs}
        if mine == _added_by.get(by, {}):
            return False
        everyone = {**_added_by, by: mine}
        new = {a: p for who in everyone.values() for a, p in who.items()}
        if len(new) != sum(len(who) for who in everyone.values()):
            raise ValueError(f"Two producers added share an artifact: {sorted(mine)}")
        for p in mine.values():
            _check_added(p, own)
        _check([*own.values(), *new.values()], added=frozenset(new))
        _check_names([*own.values(), *new.values()], added=frozenset(new))
        _added_by = {who: added for who, added in everyone.items() if added}
        _added = new
        return True


def _check_added(p: ProducerSpec, own: dict[str, ProducerSpec]) -> None:
    """An added producer is a plug-in's work, named by its plug-in's id (ADR-024), which the scheduler runs from
    what Lumiverb keeps: it reads no original, makes nothing at a scan, uses no AI job, can always be made again
    and is no part of another."""
    if not (PLUGIN_ID.match(p.artifact) and p.artifact == p.plugin) or p.artifact in own:
        raise ValueError(f"{p.artifact}: an added producer's artifact is its plug-in's id, none of Lumiverb's own")
    if not p.scheduled or p.storage or p.made_at_scan or p.job or p.cant_redo or p.part_of or p.regroup \
            or p.uniform or p.redo_also:
        raise ValueError(f"{p.artifact}: an added producer is run by the scheduler from what Lumiverb keeps"
                         " (no storage, made_at_scan, AI job, cant_redo, part_of, regroup, uniform or redo_also)")
    if p.pool is None or p.pool.name in pools_of(own.values()) or p.pool.sized_by or p.pool.gpu_hold:
        raise ValueError(f"{p.artifact}: an added producer's pool is its own, so many jobs at once"
                         " (none of Lumiverb's, no sized_by or gpu_hold)")


def _check(found: list[ProducerSpec], added: frozenset[str] = frozenset()) -> None:
    """What would otherwise go wrong quietly: two producers making one thing,
    one the scheduler runs missing what it needs to queue and run it, or two
    declaring one pool or AI job differently. added: those added while
    running (a plug-in's work), whose plug-in is an installed one."""
    from src.processing.machine import Machine

    for p in found:
        if p.artifact not in added and not NAME.match(p.artifact):
            raise ValueError(f"{p.artifact}: one of Lumiverb's own producers is named by what it makes (a name)")
    artifacts = [p.artifact for p in found]
    if len(set(artifacts)) != len(artifacts):
        raise ValueError(f"Two producers share a artifact: {sorted(artifacts)}")
    pools: dict[str, Pool] = {SCANS.name: SCANS}
    jobs: dict[str, AiJob] = {}
    job_pools: dict[str, str] = {}
    for p in found:
        if (p.scheduled or p.pool is not None) and not (":" in p.run and p.pool):
            raise ValueError(f"{p.artifact}: a producer the scheduler runs needs a run and a pool")
        if p.unit not in ("second", "clip"):
            raise ValueError(f"{p.artifact}: its unit is a second (of video) or a clip, not {p.unit!r}")
        levels = [s for s in p.settings if s.kind == "level"]
        if len(levels) > 1 or any(s.remakes or s.default not in s.choices for s in levels):
            raise ValueError(f"{p.artifact}: one level setting at most, kept out of the hash"
                             " (remakes=False), its default among its choices")
        others = {s.key for s in p.settings}
        for s in p.settings:
            if s.kind != "notch":
                continue
            stands_for = {tuple(sorted(m)) for m in s.means.values()}
            if not s.remakes or s.default not in s.choices or set(s.means) != set(s.choices) \
                    or len(stands_for) != 1 or set(next(iter(stands_for))) & others:
                raise ValueError(f"{p.artifact}: its notch {s.key!r} is in the hash (remakes=True), its default"
                                 " among its choices, each choice meaning the same keys, none another setting's")
        if p.plugin and p.artifact not in added:
            raise ValueError(f"{p.artifact}: only an installed plug-in's work belongs to a plug-in, not {p.plugin!r}")
        if p.part_of and p.part_of not in {q.artifact for q in found}:
            raise ValueError(f"{p.artifact}: it's part of {p.part_of!r}, which no producer makes")
        missing = [n for n in (*p.needs, *p.redo_also) if n not in {q.artifact for q in found}]
        if missing:
            raise ValueError(f"{p.artifact} names what no producer makes: {missing}")
        if p.pool is None:
            continue
        if pools.setdefault(p.pool.name, p.pool) != p.pool:
            raise ValueError(f"{p.artifact}: the pool {p.pool.name!r} is declared another way elsewhere")
        if p.pool.sized_by and not hasattr(Machine(), p.pool.sized_by):
            raise ValueError(f"{p.artifact}: no setting of this machine's sizes a pool: {p.pool.sized_by!r}")
        if p.pool.job is not None:
            job = p.pool.job
            if jobs.setdefault(job.name, job) != job:
                raise ValueError(f"{p.artifact}: the AI job {job.name!r} is declared another way elsewhere")
            if job_pools.setdefault(job.name, p.pool.name) != p.pool.name:
                raise ValueError(f"{p.artifact}: the AI job {job.name!r} has one pool (its machines)")
            if p.setting("model") is None:
                raise ValueError(f"{p.artifact}: a producer an AI job makes takes its model (a setting \"model\")")


def _check_names(found: list[ProducerSpec], added: frozenset[str] = frozenset()) -> None:
    """ADR-024's rules for names: each of Lumiverb's own producers makes one, `lumiverb.` then words, under its
    media (`lumiverb.image.*`, `lumiverb.video.*`, or neither for both); no name made twice; every name read
    is made (by a producer, or by Lumiverb itself: names.LUMIVERB); no circle; one that reads the originals
    says so; and no two names spelt alike for SQL (names.sql_spelling). added: those added while running (a
    plug-in's work), which name nothing they make in phase 1, only what they read."""
    made: dict[str, str] = {d.name: "Lumiverb" for d in LUMIVERB}
    reads: dict[str, tuple[str, ...]] = {d.name: d.reads for d in LUMIVERB}
    for p in found:
        if p.artifact in added:
            continue
        if p.makes is None:
            raise ValueError(f"{p.artifact}: one of Lumiverb's own producers names what it makes (makes)")
        name = p.makes.name
        if not NAME.match(name):
            raise ValueError(f"{p.artifact}: {name!r} isn't a name: lumiverb, then words, dots between")
        level = name.split(".")[1]
        want = {("image",): "image", ("video",): "video"}.get(tuple(p.media), "")
        if (level in ("image", "video") or want) and level != want:
            raise ValueError(f"{p.artifact}: {name} is under its media's level only when one kind of clip has it"
                             f" ({'lumiverb.' + want + '.*' if want else 'no media level'})")
        if name in made:
            raise ValueError(f"{p.artifact}: {name} is made by {made[name]} already")
        if name != p.artifact:
            raise ValueError(f"{p.artifact}: a producer's artifact is the name of what it makes, {name}")
        if p.storage and ORIGINAL not in p.reads:
            raise ValueError(f"{p.artifact}: it reads the originals (storage=True): its reads say {ORIGINAL}")
        made[name] = p.artifact
        reads[name] = p.reads
    for p in found:
        missing = [n for n in (*p.reads, *p.reads_others) if n not in made]
        if missing:
            raise ValueError(f"{p.artifact} reads what nothing makes: {missing}")
    spelt: dict[str, str] = {}
    for name in made:
        if spelt.setdefault(sql_spelling(name), name) != name:
            raise ValueError(f"{name} and {spelt[sql_spelling(name)]} are spelt alike in SQL")

    def visit(name: str, path: tuple[str, ...]) -> None:
        if name in path:
            raise ValueError(f"A circle: {' → '.join((*path[path.index(name):], name))}")
        for n in reads.get(name, ()):
            visit(n, (*path, name))

    for name in made:
        visit(name, ())


def pools() -> dict[str, Pool]:
    """Every pool by name: the scan pass's, then each producer's."""
    return pools_of(registry().values())


def pools_of(producers: Iterable[ProducerSpec]) -> dict[str, Pool]:
    out: dict[str, Pool] = {SCANS.name: SCANS}
    for p in producers:
        if p.pool is not None:
            out.setdefault(p.pool.name, p.pool)
    return out


def jobs() -> dict[str, AiJob]:
    """Every AI job the producers' machines do, by name (Settings → AI)."""
    return {p.pool.job.name: p.pool.job for p in registry().values() if p.pool is not None and p.pool.job}


def load(path: str):
    """What a "module:name" path names (a producer's work, a guard, a regroup)."""
    module, _, name = path.partition(":")
    return getattr(importlib.import_module(module), name)
