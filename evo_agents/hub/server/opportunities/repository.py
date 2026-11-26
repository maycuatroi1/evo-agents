"""The opportunities of a project in the database: tables opportunities, opportunity_revisions and opportunity_answers
(``OpportunityStore``), and the problems and evidence an opportunity names."""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import and_, func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import opportunities, tables
from evo_agents.hub.server.base.revisions import RevisionedStore
from evo_agents.hub.server.opportunities.models import Answer
from evo_agents.hub.server.opportunity_deadlines import closing_filter


def _select():
    """An opportunity at a revision, with its owner's and writer's logins."""
    o, r = tables.opportunities, tables.opportunity_revisions
    owner, writer = tables.users.alias("owner"), tables.users.alias("writer")
    return (
        select(
            o.c.id,
            o.c.number,
            r.c.revision,
            r.c.statement,
            r.c.beneficiary,
            r.c.leverage,
            r.c.window_until,
            r.c.window_note,
            r.c.metric,
            r.c.rank,
            r.c.owner_id,
            owner.c.login.label("owner"),
            r.c.status,
            r.c.close_reason,
            r.c.note,
            writer.c.login.label("created_by"),
            r.c.created_at,
        )
        .join_from(o, r, r.c.opportunity_id == o.c.id)
        .join(owner, owner.c.id == r.c.owner_id)
        .join(writer, writer.c.id == r.c.created_by)
    )


async def answers_of(conn: AsyncConnection, opportunity_id: int, revision: int) -> list[Answer]:
    """The problems revision ``revision`` of the opportunity answers, in the order the writer gave them."""
    a, p, j = tables.opportunity_answers, tables.problems, tables.projects
    query = (
        select(p.c.id, j.c.id.label("project_id"), j.c.name, p.c.number)
        .join_from(a, p, p.c.id == a.c.problem_id)
        .join(j, j.c.id == p.c.project_id)
        .where(a.c.opportunity_id == opportunity_id, a.c.revision == revision)
        .order_by(a.c.position)
    )
    return [Answer(row.id, row.project_id, row.name, row.number) for row in await conn.execute(query)]


async def _with_answers(conn: AsyncConnection, rows) -> list:
    """Each of ``rows`` (from ``_select``) with the problems its revision answers, as ``answers``."""
    return [SimpleNamespace(**row._mapping, answers=await answers_of(conn, row.id, row.revision)) for row in rows]


class OpportunityStore(RevisionedStore):
    lock_name = "opportunities"
    noun = "opportunity"
    head = tables.opportunities
    revisions = tables.opportunity_revisions
    key = "opportunity_id"

    def code(self, number: int) -> str:
        return opportunities.code(number)

    def number_of(self, ref: str) -> int | None:
        return opportunities.number_of(ref)

    async def found(self, conn: AsyncConnection, project_id: int, ref: str, revision: int | None = None):
        """Opportunity ``ref`` of the project at ``revision`` (its newest when None), its row with ``answers``; None
        when there is no such one."""
        o = tables.opportunities
        at = o.c.revision if revision is None else revision
        number = opportunities.number_of(ref)
        query = _select().where(
            o.c.project_id == project_id, o.c.number == number, tables.opportunity_revisions.c.revision == at
        )
        row = (await conn.execute(query)).first()
        if row is None:
            return None
        return SimpleNamespace(**row._mapping, answers=await answers_of(conn, row.id, row.revision))

    def stored(self, row) -> dict:
        return {
            "statement": row.statement,
            "beneficiary": row.beneficiary,
            "leverage": row.leverage,
            "window_until": row.window_until,
            "window_note": row.window_note,
            "answers": [answer.problem_id for answer in row.answers],
            "metric": row.metric,
            "rank": row.rank,
            "owner_id": row.owner_id,
            "status": row.status,
            "close_reason": row.close_reason,
        }

    def revision_row(self, values: dict) -> dict:
        """The answers live in table opportunity_answers."""
        return {k: v for k, v in values.items() if k != "answers"}

    async def inserted(self, conn: AsyncConnection, record_id: int, revision: int, values: dict) -> None:
        a = tables.opportunity_answers
        for position, problem_id in enumerate(values["answers"], start=1):
            row = {"opportunity_id": record_id, "revision": revision, "position": position, "problem_id": problem_id}
            await conn.execute(insert(a).values(row))

    async def next_rank(self, conn: AsyncConnection, project_id: int) -> int:
        """After the project's open opportunities."""
        o, r = tables.opportunities, tables.opportunity_revisions
        query = (
            select(func.coalesce(func.max(r.c.rank), 0))
            .join_from(o, r, and_(r.c.opportunity_id == o.c.id, r.c.revision == o.c.revision))
            .where(o.c.project_id == project_id, r.c.status == opportunities.OPEN)
        )
        return min(int(await conn.scalar(query) or 0) + 1, opportunities.MAX_RANK)

    async def listed(
        self, conn: AsyncConnection, project_id: int, status: str | None = None, closing_within: int | None = None
    ) -> list:
        """The project's opportunities at their newest revision, with their answers, by rank then code; those of
        ``status`` alone when given; with ``closing_within`` the open ones whose window ends within that many days,
        nearest end first."""
        o, r = tables.opportunities, tables.opportunity_revisions
        query = _select().where(o.c.project_id == project_id, r.c.revision == o.c.revision)
        if status is not None:
            query = query.where(r.c.status == status)
        if closing_within is not None:
            query = closing_filter(query, closing_within).order_by(r.c.window_until, r.c.rank, o.c.number)
        else:
            query = query.order_by(r.c.rank, o.c.number)
        return await _with_answers(conn, (await conn.execute(query)).all())

    async def revisions_of(self, conn: AsyncConnection, opportunity_id: int) -> list:
        """Every revision of opportunity ``opportunity_id``, with its answers, the oldest first."""
        o, r = tables.opportunities, tables.opportunity_revisions
        rows = (await conn.execute(_select().where(o.c.id == opportunity_id).order_by(r.c.revision))).all()
        return await _with_answers(conn, rows)


STORE = OpportunityStore()


async def evidence_sources(conn: AsyncConnection, opportunity_id: int) -> list:
    """The kind and address of the source of each line of evidence of opportunity ``opportunity_id``."""
    e = tables.opportunity_evidence
    query = select(e.c.source_kind, e.c.source_uri).where(e.c.opportunity_id == opportunity_id)
    return list((await conn.execute(query)).all())


async def problem_of(conn: AsyncConnection, project_id: int, number: int):
    """The id and status of problem ``number`` of the project; None without one."""
    p, r = tables.problems, tables.problem_revisions
    query = (
        select(p.c.id, r.c.status)
        .join_from(p, r, and_(r.c.problem_id == p.c.id, r.c.revision == p.c.revision))
        .where(p.c.project_id == project_id, p.c.number == number)
    )
    return (await conn.execute(query)).first()
