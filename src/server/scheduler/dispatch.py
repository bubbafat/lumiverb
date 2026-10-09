"""Which job runs next (ADR-016 phase 4).

The scheduler keeps one ranked queue of every job, in every account: by
tier (1 see it, 2 prepare, 3 find it, 4 redo), then oldest first. Each
resource is a pool with so many slots (the probes, the renders, the vision
machines' requests at once, ...); a free slot takes the best job that uses
it, so a lower tier fills what a higher one leaves idle.

The jobs come from the database (queue.py): each kind's due clips, a
buffer's worth at a time, offered here. A job in hand isn't offered again,
and one just tried isn't taken again for a while. A clip saved, or
reported as failing (the server then says when to try it again), only
until the database has it (settle_after: a listing read before the save
still names it). A clip that waits without saying why (the GPU ran out of
memory, its file changed since it was hashed) would otherwise be first
again at once: it's held for retake_after. The database is asked to leave
held clips out (held()), so they can't fill its answer and keep the clips
behind them waiting. A job that couldn't try at all (the storage went
away) holds nothing back.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass

# A kind's buffer is topped up once fewer than this are waiting.
LOW = 10
# A kind whose every due clip is known (the database listed fewer than it
# was asked for) isn't asked again for this long, doubling while it keeps
# having nothing new, up to EMPTY_WAIT_MAX.
EMPTY_WAIT = 15.0
EMPTY_WAIT_MAX = 120.0


@dataclass(frozen=True)
class KindSpec:
    tier: int
    pool: str
    batch: int = 1  # items per job (faces: one subprocess call)
    retake_after: float | None = None  # the dispatcher's default when None
    # The pool is each account's own (its AI machines), named "<pool>@<account>".
    per_account: bool = False
    # Kinds that make the same thing share one: a clip is never in hand for
    # two of them at once (descriptions, and redoing descriptions).
    same_as: str = ""


@dataclass(frozen=True)
class Job:
    tenant_id: str
    kind: str
    tier: int
    items: tuple[dict, ...]

    @property
    def asset_ids(self) -> list[str]:
        return [i["asset_id"] for i in self.items]


def _same(kind: str, spec: KindSpec) -> str:
    return spec.same_as or kind


# What a job says when it's done: "not_tried" (runners.NOT_TRIED: it couldn't
# try at all), None (each clip saved or reported as failing), or the clips
# that wait without either (held the long while).
Outcome = str | list[str] | None


def pool_key(spec: KindSpec, tenant_id: str) -> str:
    return f"{spec.pool}@{tenant_id}" if spec.per_account else spec.pool


def _order(tier: int, item: dict) -> tuple:
    return (tier, str(item.get("created_at") or ""), item["asset_id"])


class Dispatcher:
    def __init__(self, kinds: Mapping[str, KindSpec], capacity: Mapping[str, int], *,
                 retake_after: float = 3600.0, settle_after: float = 60.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._kinds = dict(kinds)
        self._capacity = dict(capacity)
        self._retake_after = retake_after
        self.settle_after = settle_after
        self._clock = clock
        self._lock = threading.Lock()
        self._buffers: dict[tuple[str, str], list[dict]] = {}
        self._all_known_at: dict[tuple[str, str], float] = {}
        self._empty_wait: dict[tuple[str, str], float] = {}
        self._in_hand: set[tuple[str, str, str]] = set()
        self._taken_until: dict[tuple[str, str, str], float] = {}
        self._busy: dict[str, int] = {}
        self._running_kinds: dict[tuple[str, str], int] = {}

    # -- capacity -----------------------------------------------------------

    def set_capacity(self, pool: str, slots: int) -> None:
        with self._lock:
            self._capacity[pool] = max(0, slots)

    def free(self, pool: str) -> int:
        with self._lock:
            return self._free(pool)

    def _free(self, pool: str) -> int:
        return max(0, self._capacity.get(pool, 0) - self._busy.get(pool, 0))

    def running(self, pool: str) -> int:
        with self._lock:
            return self._busy.get(pool, 0)

    def waiting(self, pool: str) -> int:
        with self._lock:
            return sum(len(b) for (tenant_id, kind), b in self._buffers.items()
                       if pool_key(self._kinds[kind], tenant_id) == pool)

    def pools(self, tenant_ids: list[str]) -> list[str]:
        """Every pool's key: the shared ones, and each account's own."""
        out: list[str] = []
        for spec in self._kinds.values():
            for key in ([pool_key(spec, t) for t in tenant_ids] if spec.per_account else [spec.pool]):
                if key not in out:
                    out.append(key)
        return out

    # -- the queue ----------------------------------------------------------

    def wanted(self, tenant_id: str, kind: str) -> bool:
        """Whether to ask the database for more of this kind now."""
        key = (tenant_id, kind)
        with self._lock:
            known_at = self._all_known_at.get(key)
            if known_at is not None and self._clock() - known_at < self._empty_wait.get(key, EMPTY_WAIT):
                return False
            return len(self._buffers.get(key, ())) < LOW

    def offer(self, tenant_id: str, kind: str, items: list[dict], *, complete: bool = True) -> None:
        """The kind's due clips as the database lists them now, oldest
        first: they replace what was waiting. complete: that's all of them
        (fewer than asked for), so it isn't asked again for a while."""
        key = (tenant_id, kind)
        tier = self._kinds[kind].tier
        now = self._clock()
        with self._lock:
            self._taken_until = {k: t for k, t in self._taken_until.items() if t > now}
            same = _same(kind, self._kinds[kind])
            fresh = [i for i in items
                     if (tenant_id, same, i["asset_id"]) not in self._in_hand
                     and (tenant_id, same, i["asset_id"]) not in self._taken_until]
            fresh.sort(key=lambda i: _order(tier, i))
            self._buffers[key] = fresh
            if complete or not fresh:
                self._all_known_at[key] = now
                # Nothing new again: ask less often, up to EMPTY_WAIT_MAX.
                last = None if fresh else self._empty_wait.get(key)
                if fresh:
                    self._empty_wait.pop(key, None)
                else:
                    self._empty_wait[key] = EMPTY_WAIT if last is None else min(EMPTY_WAIT_MAX, last * 2)
            else:
                self._all_known_at.pop(key, None)
                self._empty_wait.pop(key, None)

    def clear(self, tenant_id: str, kind: str) -> None:
        """Hand out none of this kind for now (its machines can't do it)."""
        with self._lock:
            self._buffers.pop((tenant_id, kind), None)

    def take(self, pool: str) -> Job | None:
        """The best job a free slot of this pool can run, now in hand; None
        if none. pool: a pool's name, or "<pool>@<account>" for an account's own."""
        with self._lock:
            if self._free(pool) <= 0:
                return None
            best: tuple | None = None
            for (tenant_id, kind), buffer in self._buffers.items():
                spec = self._kinds[kind]
                if pool_key(spec, tenant_id) != pool:
                    continue
                # A clip another kind took meanwhile (its redo, say) waits for it.
                same = _same(kind, spec)
                buffer[:] = [i for i in buffer if (tenant_id, same, i["asset_id"]) not in self._in_hand]
                if not buffer:
                    continue
                rank = _order(spec.tier, buffer[0])
                if best is None or rank < best[0]:
                    best = (rank, tenant_id, kind)
            if best is None:
                return None
            _, tenant_id, kind = best
            spec = self._kinds[kind]
            buffer = self._buffers[(tenant_id, kind)]
            items, self._buffers[(tenant_id, kind)] = buffer[:spec.batch], buffer[spec.batch:]
            for i in items:
                self._in_hand.add((tenant_id, _same(kind, spec), i["asset_id"]))
            self._busy[pool] = self._busy.get(pool, 0) + 1
            self._running_kinds[(tenant_id, kind)] = self._running_kinds.get((tenant_id, kind), 0) + 1
            return Job(tenant_id, kind, spec.tier, tuple(items))

    def status(self, tenant_id: str) -> dict:
        """What's running and waiting for the account, per kind, and each of
        its pools' slots (the shared ones and its own): Settings → Processing shows it."""
        with self._lock:
            running = {k: n for (t, k), n in self._running_kinds.items() if t == tenant_id and n}
            waiting = {k: len(b) for (t, k), b in self._buffers.items() if t == tenant_id and b}
            pools = {}
            for spec in self._kinds.values():
                key = pool_key(spec, tenant_id)
                pools[spec.pool] = [self._busy.get(key, 0), self._capacity.get(key, 0)]
            return {"running": running, "waiting": waiting, "pools": pools}

    def held(self, tenant_id: str, kind: str) -> list[str]:
        """The account's clips of this kind in hand or just tried: what the
        database is asked to leave out."""
        same = _same(kind, self._kinds[kind])
        now = self._clock()
        with self._lock:
            return [k[2] for k in self._in_hand | {k for k, t in self._taken_until.items() if t > now}
                    if k[0] == tenant_id and k[1] == same]

    def forget_taken(self, tenant_id: str) -> None:
        """Someone asked for failing clips to be tried again: none of the
        account's clips waits out its hour any more."""
        with self._lock:
            self._taken_until = {k: t for k, t in self._taken_until.items() if k[0] != tenant_id}
            for key in [k for k in self._all_known_at if k[0] == tenant_id]:
                del self._all_known_at[key]

    def done(self, job: Job, *, tried: bool = True, waiting: Collection[str] | None = None) -> None:
        """The job's slot is free; its clips aren't taken again for a while
        (unless it couldn't try at all). waiting: the clips neither saved nor
        reported (None: it can't say, so all of them)."""
        spec = self._kinds[job.kind]
        wait = self._retake_after if spec.retake_after is None else spec.retake_after
        pool = pool_key(spec, job.tenant_id)
        waits = set(job.asset_ids if waiting is None else waiting)
        with self._lock:
            self._busy[pool] = max(0, self._busy.get(pool, 0) - 1)
            key = (job.tenant_id, job.kind)
            self._running_kinds[key] = max(0, self._running_kinds.get(key, 0) - 1)
            now = self._clock()
            for asset_id in job.asset_ids:
                key = (job.tenant_id, _same(job.kind, spec), asset_id)
                self._in_hand.discard(key)
                if tried:
                    self._taken_until[key] = now + (wait if asset_id in waits else min(wait, self.settle_after))
