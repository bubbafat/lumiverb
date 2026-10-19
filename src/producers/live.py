"""Tables worked out from the producers, worked out again when the list
changes (a plug-in's work added while Lumiverb runs: src/producers/__init__.py).

Each is read as the table it stands for (a mapping, a sequence or a set) and
is made from ``registry()`` as it is when read: the first read after the
list changes makes it again. A caller that reads one table twice in a row
can, rarely, see two lists (one added between): what's added changes only
when the server admin installs or removes a plug-in.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence, Set
from typing import Any

from src.producers import registry


class Made[T]:
    """What build makes from the producers, kept until the list changes."""

    def __init__(self, build: Callable[[], T]) -> None:
        self._build = build
        self._made: tuple[object, T] | None = None

    def now(self) -> T:
        source = registry()
        made = self._made
        if made is None or made[0] is not source:
            made = self._made = (source, self._build())
        return made[1]


class LiveMap[K, V](Mapping[K, V]):
    def __init__(self, build: Callable[[], Mapping[K, V]]) -> None:
        self._made = Made(build)

    def __getitem__(self, key: K) -> V:
        return self._made.now()[key]

    def __iter__(self) -> Iterator[K]:
        return iter(self._made.now())

    def __len__(self) -> int:
        return len(self._made.now())

    def __contains__(self, key: object) -> bool:
        return key in self._made.now()

    # One list throughout: the made table's own views, never a lookup per key.
    def keys(self):  # type: ignore[override]
        return self._made.now().keys()

    def values(self):  # type: ignore[override]
        return self._made.now().values()

    def items(self):  # type: ignore[override]
        return self._made.now().items()

    def get(self, key: Any, default: Any = None) -> Any:
        return self._made.now().get(key, default)

    def __repr__(self) -> str:
        return f"LiveMap({dict(self._made.now())!r})"


class LiveSeq[T](Sequence[T]):
    def __init__(self, build: Callable[[], Sequence[T]]) -> None:
        self._made = Made(lambda: tuple(build()))

    def __getitem__(self, index: Any) -> Any:
        return self._made.now()[index]

    def __iter__(self) -> Iterator[T]:
        return iter(self._made.now())

    def __len__(self) -> int:
        return len(self._made.now())

    def __contains__(self, value: object) -> bool:
        return value in self._made.now()

    def __eq__(self, other: object) -> bool:
        return tuple(self) == (tuple(other) if isinstance(other, Sequence) else other)

    def __hash__(self) -> int:
        return hash(self._made.now())

    def __repr__(self) -> str:
        return f"LiveSeq({self._made.now()!r})"


class LiveSet[T](Set[T]):
    def __init__(self, build: Callable[[], Set[T]]) -> None:
        self._made = Made(lambda: frozenset(build()))

    def __iter__(self) -> Iterator[T]:
        return iter(self._made.now())

    def __len__(self) -> int:
        return len(self._made.now())

    def __contains__(self, value: object) -> bool:
        return value in self._made.now()

    def __repr__(self) -> str:
        return f"LiveSet({set(self._made.now())!r})"
