"""A record of a project kept as revisions: a row of a head table (``goals``) that holds the record's number in the
project and its newest revision, and a row of its revisions table (``goal_revisions``) for each revision.

``RevisionedStore.revise`` is the write, a Template Method: it holds the advisory lock of the project's records until
the transaction ends, so writes of one project wait for each other; reads the record as it is now (``found``);
answers 409 when the writer wanted another revision than the one now (``if_revision``; 0 for a new record only);
takes the number of a new record (``number_of_new``: the next one); asks the caller for the values of the next
revision, which raises the 422 of a wrong write; writes nothing when they are what the record holds already
(``stored``); and otherwise writes the next revision (``insert``, then ``inserted`` for the rows a revision holds
besides its own). A subclass names its tables, its noun and its codes, and reads its records.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from sqlalchemy import Table, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server.base.errors import Conflict, Unprocessable


class Project(Protocol):
    """What a store reads of a member's access to a project (``projects.ProjectAccess``): its id and its name. The
    store imports nothing of the server above ``base``, so the repository of a package stays below its service."""

    @property
    def project_id(self) -> int: ...

    @property
    def name(self) -> str: ...


@dataclass(frozen=True)
class Revised:
    """What ``revise`` did: the record before (None for a new one) and after, and whether it wrote a revision."""

    before: Any
    after: Any
    changed: bool


def at_revision(what: str, now: int, wanted: int | None) -> None:
    """409 when a writer wanted ``what`` at revision ``wanted`` and it is at ``now``; nothing when they wanted none."""
    if wanted not in (None, now):
        raise Conflict(f"{what} is at revision {now}, not {wanted}; nothing changed")


class RevisionedStore(ABC):
    lock_name: ClassVar[str]  # "goals": the advisory lock is "<lock_name>:<project id>"
    noun: ClassVar[str]  # "goal", as a refusal names one
    head: ClassVar[Table]
    revisions: ClassVar[Table]
    key: ClassVar[str]  # the column of the revisions table naming the head's id: "goal_id"

    @abstractmethod
    def code(self, number: int) -> str:
        """The code of record ``number``: G-<number>."""

    @abstractmethod
    def number_of(self, ref: str) -> int | None:
        """The number code ``ref`` names; None for a name that is no code."""

    @abstractmethod
    async def found(self, conn: AsyncConnection, project_id: int, ref: str, revision: int | None = None) -> Any:
        """Record ``ref`` of the project at ``revision`` (its newest when None); None when there is no such one."""

    @abstractmethod
    def stored(self, row) -> dict:
        """What revision ``row`` holds, as the values of a write give it."""

    async def lock(self, conn: AsyncConnection, project_id: int) -> None:
        """Hold, until the transaction ends, the lock that serialises the writes of the project's records."""
        name = f"{self.lock_name}:{project_id}"
        await conn.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(name, 0))))

    async def next_number(self, conn: AsyncConnection, project_id: int) -> int:
        h = self.head
        query = select(func.coalesce(func.max(h.c.number), 0)).where(h.c.project_id == project_id)
        return int(await conn.scalar(query) or 0) + 1

    async def next_code(self, conn: AsyncConnection, project_id: int) -> str:
        """The code the project's next record takes."""
        return self.code(await self.next_number(conn, project_id))

    async def number_of_new(self, conn: AsyncConnection, access: Project, ref: str) -> int:
        """The number a new record named ``ref`` takes: the next one; 422 for another code."""
        following = await self.next_number(conn, access.project_id)
        if self.number_of(ref) != following:
            raise Unprocessable(
                f"the next {self.noun} of project {access.name} is {self.code(following)}, not {ref}; nothing changed"
            )
        return following

    def revision_row(self, values: dict) -> dict:
        """The columns of the revisions table among ``values``: all of them, unless another table holds some."""
        return values

    async def inserted(self, conn: AsyncConnection, record_id: int, revision: int, values: dict) -> None:
        """Write what revision ``revision`` holds outside the revisions table; nothing by default."""
        return None

    async def insert(
        self,
        conn: AsyncConnection,
        project_id: int,
        number: int,
        revision: int,
        values: dict,
        note: str | None,
        writer: int,
        head: dict | None = None,
    ) -> int:
        """Write revision ``revision`` of record ``number`` of the project, the record itself with its first (with
        ``head``, more columns of it); the record's id."""
        h = self.head
        if revision == 1:
            row = {"project_id": project_id, "number": number, "revision": 1, **(head or {})}
            record_id = (await conn.execute(insert(h).values(row).returning(h.c.id))).scalar_one()
        else:
            query = update(h).where(h.c.project_id == project_id, h.c.number == number).values(revision=revision)
            record_id = (await conn.execute(query.returning(h.c.id))).scalar_one()
        written = {**self.revision_row(values), self.key: record_id, "revision": revision, "note": note}
        await conn.execute(insert(self.revisions).values({**written, "created_by": writer}))
        await self.inserted(conn, record_id, revision, values)
        return record_id

    async def revise(
        self,
        conn: AsyncConnection,
        access: Project,
        ref: str,
        if_revision: int | None,
        values_of: Callable[[Any], Awaitable[dict]],
        *,
        note: str | None,
        writer: int,
    ) -> Revised:
        """Write the next revision of record ``ref`` of the project, with the values ``values_of`` gives for the
        record as it is now (None for a new one), while it is at ``if_revision`` (409 otherwise)."""
        await self.lock(conn, access.project_id)
        held = await self.found(conn, access.project_id, ref)
        now = 0 if held is None else held.revision
        at_revision(f"{self.noun} {ref} of project {access.name}", now, if_revision)
        number = held.number if held is not None else await self.number_of_new(conn, access, ref)
        values = await values_of(held)
        if held is not None and values == self.stored(held):
            return Revised(held, held, False)
        note = " ".join((note or "").split()) or None
        await self.insert(conn, access.project_id, number, now + 1, values, note, writer)
        return Revised(held, await self.found(conn, access.project_id, self.code(number)), True)
