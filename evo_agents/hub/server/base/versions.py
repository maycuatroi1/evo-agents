"""A record whose write names the version it was made on, the variant of ``revisions`` for a record that keeps its
version on its own row: a document saved as its next version (docs, srs: ``base_version``), a claim of a canvas
whose rev goes up (canvas: ``base_rev``).

``VersionedStore.save`` is the write, a Template Method: it reads the record with its row locked until the
transaction ends (``held``, None for a new one); checks the version the writer read against the one now (``check``:
by default a refusal from ``stale`` unless they are the same; a store that merges a write made on an older version
into the latest, as srs does, checks less); asks the caller for the values of the next version; writes nothing when
they are what the record holds already (``unchanged``); and otherwise writes the next version (``insert``, 1 for a
new record). ``check_all`` checks many records of one write at once and refuses with every stale one
(``refusal``), as an edit of several claims does. A subclass names its noun, reads and writes its rows, and says
what a stale write answers; the errors it raises are its domain's.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Generic, TypeVar

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server.base.errors import Conflict

K = TypeVar("K")
R = TypeVar("R")


@dataclass(frozen=True)
class Versioned(Generic[R]):
    """What ``save`` did: the record before (None for a new one), the version it is at now, whether it wrote one."""

    before: R | None
    version: int
    changed: bool


class VersionedStore(ABC, Generic[K, R]):
    noun: ClassVar[str]  # "document", as a refusal names one

    @abstractmethod
    async def held(self, conn: AsyncConnection, key: K) -> R | None:
        """Record ``key`` with its row locked until the transaction ends; None when there is no such one."""

    @abstractmethod
    def version_of(self, row: R) -> int:
        """The version record ``row`` is at now."""

    async def stale(self, conn: AsyncConnection, key: K, row: R, base: int) -> Any:
        """What a writer reads of record ``row``, at another version than ``base``, the one they read."""
        return f"{self.noun} {key} is at version {self.version_of(row)}, not {base}"

    def refusal(self, stale: list) -> Exception:
        """The error of a write made on versions that are not the ones now, with what ``stale`` said of each."""
        return Conflict(f"{'; '.join(map(str, stale))}; nothing changed")

    def missing(self, key: K, base: int) -> Exception:
        """The error of a write made on version ``base`` of a record there is no such one of."""
        return Conflict(f"there is no {self.noun} {key}; nothing changed")

    async def check(self, conn: AsyncConnection, key: K, row: R | None, base: int | None) -> None:
        """Refuse a write made on version ``base`` of ``row`` (None: on none named) unless it is the version now."""
        if row is None:
            if base:
                raise self.missing(key, base)
            return
        if base is not None and base != self.version_of(row):
            raise self.refusal([await self.stale(conn, key, row, base)])

    async def check_all(self, conn: AsyncConnection, held: Mapping[K, R], asked: Mapping[K, int]) -> None:
        """Refuse, with every one of them, a write made on records of ``held`` at other versions than ``asked``."""
        found = [
            await self.stale(conn, key, held[key], base)
            for key, base in asked.items()
            if base != self.version_of(held[key])
        ]
        if found:
            raise self.refusal(found)

    def unchanged(self, row: R, values: Any) -> bool:
        """Whether ``values`` are what ``row`` holds already; never, by default."""
        return False

    @abstractmethod
    async def insert(self, conn: AsyncConnection, key: K, row: R | None, version: int, values: Any) -> None:
        """Write ``values`` as version ``version`` of record ``key``: the record itself when ``row`` is None."""

    async def save(
        self,
        conn: AsyncConnection,
        key: K,
        base: int | None,
        values_of: Callable[[R | None], Awaitable[Any]],
    ) -> Versioned[R]:
        """Write the next version of record ``key`` with the values ``values_of`` gives for it as it is now (None
        for a new one), made on version ``base``."""
        row = await self.held(conn, key)
        await self.check(conn, key, row, base)
        values = await values_of(row)
        if row is not None and self.unchanged(row, values):
            return Versioned(row, self.version_of(row), False)
        version = 1 if row is None else self.version_of(row) + 1
        await self.insert(conn, key, row, version, values)
        return Versioned(row, version, True)
