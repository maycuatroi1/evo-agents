"""The problems of a project in the database: tables problems and problem_revisions (``ProblemStore``), and what
points to a problem (its evidence, the goals that answer it, the claims and plans that name it)."""

from __future__ import annotations

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import problems, tables
from evo_agents.hub.server.base.revisions import RevisionedStore
from evo_agents.hub.server.plan_listing import step_counts


def _select():
    """A problem at a revision, with its owner's and writer's logins."""
    p, r = tables.problems, tables.problem_revisions
    owner, writer = tables.users.alias("owner"), tables.users.alias("writer")
    return (
        select(
            p.c.id,
            p.c.number,
            r.c.revision,
            r.c.statement,
            r.c.who,
            r.c.when,
            r.c.workaround,
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
        .join_from(p, r, r.c.problem_id == p.c.id)
        .join(owner, owner.c.id == r.c.owner_id)
        .join(writer, writer.c.id == r.c.created_by)
    )


class ProblemStore(RevisionedStore):
    lock_name = "problems"
    noun = "problem"
    head = tables.problems
    revisions = tables.problem_revisions
    key = "problem_id"

    def code(self, number: int) -> str:
        return problems.code(number)

    def number_of(self, ref: str) -> int | None:
        return problems.number_of(ref)

    async def found(self, conn: AsyncConnection, project_id: int, ref: str, revision: int | None = None):
        p, r = tables.problems, tables.problem_revisions
        at = p.c.revision if revision is None else revision
        number = problems.number_of(ref)
        query = _select().where(p.c.project_id == project_id, p.c.number == number, r.c.revision == at)
        return (await conn.execute(query)).first()

    def stored(self, row) -> dict:
        return {
            "statement": row.statement,
            "who": row.who,
            "when": row.when,
            "workaround": row.workaround,
            "metric": row.metric,
            "rank": row.rank,
            "owner_id": row.owner_id,
            "status": row.status,
            "close_reason": row.close_reason,
        }

    async def next_rank(self, conn: AsyncConnection, project_id: int) -> int:
        """After the project's open problems."""
        p, r = tables.problems, tables.problem_revisions
        query = (
            select(func.coalesce(func.max(r.c.rank), 0))
            .join_from(p, r, and_(r.c.problem_id == p.c.id, r.c.revision == p.c.revision))
            .where(p.c.project_id == project_id, r.c.status == problems.OPEN)
        )
        return min(int(await conn.scalar(query) or 0) + 1, problems.MAX_RANK)

    async def listed(self, conn: AsyncConnection, project_id: int, status: str | None = None) -> list:
        """The project's problems at their newest revision, by rank then code; those of ``status`` alone when given."""
        p, r = tables.problems, tables.problem_revisions
        query = _select().where(p.c.project_id == project_id, r.c.revision == p.c.revision)
        if status is not None:
            query = query.where(r.c.status == status)
        return list((await conn.execute(query.order_by(r.c.rank, p.c.number))).all())

    async def revisions_of(self, conn: AsyncConnection, problem_id: int) -> list:
        """Every revision of problem ``problem_id``, the oldest first."""
        p, r = tables.problems, tables.problem_revisions
        return list((await conn.execute(_select().where(p.c.id == problem_id).order_by(r.c.revision))).all())


STORE = ProblemStore()


async def evidence_sources(conn: AsyncConnection, problem_id: int) -> list:
    """The kind and address of the source of each line of evidence of problem ``problem_id``."""
    e = tables.problem_evidence
    return list((await conn.execute(select(e.c.source_kind, e.c.source_uri).where(e.c.problem_id == problem_id))).all())


async def answering_goals(conn: AsyncConnection, problem_id: int) -> list:
    """The goals whose newest revision answers problem ``problem_id``, by number."""
    g, r = tables.goals, tables.goal_revisions
    query = (
        select(g.c.number, r.c.revision, r.c.statement, r.c.status)
        .join_from(g, r, and_(r.c.goal_id == g.c.id, r.c.revision == g.c.revision))
        .where(r.c.problem_id == problem_id)
        .order_by(g.c.number)
    )
    return list((await conn.execute(query)).all())


async def linking_claims(conn: AsyncConnection, project_id: int, code: str) -> list:
    """The claims of the project's canvases that link problem ``code``, neither archived nor in an archived canvas, by
    canvas and number."""
    k, c = tables.canvas.claims, tables.canvas.canvases
    query = (
        select(c.c.slug, c.c.name, k.c.number, k.c.cell, k.c.status, k.c.confidence, k.c.text, k.c.label)
        .join_from(k, c, c.c.id == k.c.canvas_id)
        .where(
            k.c.project_id == project_id,
            k.c.problems.contains([code]),
            k.c.archived_at.is_(None),
            c.c.archived_at.is_(None),
        )
        .order_by(c.c.slug, k.c.number)
    )
    return list((await conn.execute(query)).all())


async def naming_plans(conn: AsyncConnection, project_id: int, code: str) -> list:
    """The plans of the project whose body names problem ``code`` in its problems, by id."""
    plans = tables.plans
    query = (
        select(
            plans.c.plan_id,
            plans.c.area,
            plans.c.label,
            plans.c.revision,
            plans.c.body["title"].astext.label("title"),
            plans.c.body["status"].astext.label("status"),
            *step_counts(plans.c.body),
        )
        .where(plans.c.project_id == project_id, plans.c.body["problems"].contains([code]))
        .order_by(plans.c.plan_id)
    )
    return list((await conn.execute(query)).all())
