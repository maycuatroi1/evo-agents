"""The evidence of a record in the database: ``EvidenceStore`` reads and writes the lines of a table that numbers them
within the record they show (``problem_evidence`` by ``problem_id``, schema 0137; ``opportunity_evidence`` by
``opportunity_id``, schema 0143, through the store of ``evo_agents.hub.server.opportunity_evidence.repository``), each
read with the record's number and the logins of who added and who confirmed it."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Table, delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


@dataclass(frozen=True)
class EvidenceStore:
    table: Table  # the lines: problem_evidence
    head: Table  # the records they show: problems
    noun: str  # "problem": the column ``<noun>_id`` of a line, and the label of the record's number when read

    @property
    def key(self):
        return self.table.c[f"{self.noun}_id"]

    def _select(self):
        e, head = self.table, self.head
        added, confirmed = tables.users.alias("added"), tables.users.alias("confirmed")
        return (
            select(
                self.key,
                e.c.number,
                head.c.number.label(self.noun),
                e.c.source_kind,
                e.c.source_uri,
                e.c.locator,
                e.c.speaker,
                e.c.excerpt,
                e.c.signal,
                e.c.label,
                added.c.login.label("added_by"),
                e.c.created_at.label("added_at"),
                confirmed.c.login.label("confirmed_by"),
                e.c.confirmed_at,
                e.c.proposal_id,
            )
            .join_from(e, head, head.c.id == self.key)
            .join(added, added.c.id == e.c.added_by)
            .outerjoin(confirmed, confirmed.c.id == e.c.confirmed_by)
        )

    async def lines(self, conn: AsyncConnection, record_id: int) -> list:
        """The lines of record ``record_id`` by number."""
        query = self._select().where(self.key == record_id).order_by(self.table.c.number)
        return list((await conn.execute(query)).all())

    async def line(self, conn: AsyncConnection, record_id: int, number: int):
        """Line ``number`` of record ``record_id``; None when it has none."""
        query = self._select().where(self.key == record_id, self.table.c.number == number)
        return (await conn.execute(query)).first()

    async def held(self, conn: AsyncConnection, record_id: int) -> list:
        """What tells the lines of record ``record_id`` apart (source, place, excerpt), with their numbers."""
        e = self.table
        query = select(e.c.number, e.c.source_kind, e.c.source_uri, e.c.locator, e.c.excerpt)
        return list((await conn.execute(query.where(self.key == record_id))).all())

    async def last_number(self, conn: AsyncConnection, record_id: int) -> int:
        """The number of the newest line of record ``record_id``; 0 when it has none."""
        query = select(func.coalesce(func.max(self.table.c.number), 0)).where(self.key == record_id)
        return int((await conn.execute(query)).scalar_one())

    async def insert(self, conn: AsyncConnection, values: dict) -> None:
        await conn.execute(insert(self.table).values(values))

    async def confirm(self, conn: AsyncConnection, record_id: int, number: int, user_id: int) -> None:
        stamp = {"confirmed_by": user_id, "confirmed_at": func.now()}
        e = self.table
        await conn.execute(update(e).where(self.key == record_id, e.c.number == number).values(stamp))

    async def remove_proposed(self, conn: AsyncConnection, record_id: int, proposal_id: int) -> list[int]:
        """Delete the lines proposal ``proposal_id`` made in record ``record_id``; their numbers."""
        e = self.table
        query = delete(e).where(self.key == record_id, e.c.proposal_id == proposal_id).returning(e.c.number)
        return list((await conn.execute(query)).scalars().all())

    async def number_of(self, conn: AsyncConnection, record_id: int) -> int:
        """The number of record ``record_id`` in its project."""
        return (await conn.execute(select(self.head.c.number).where(self.head.c.id == record_id))).scalar_one()


STORE = EvidenceStore(tables.problem_evidence, tables.problems, "problem")
