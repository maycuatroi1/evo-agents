"""The goals of a project in the database: tables goals and goal_revisions (``GoalStore``), and what a goal names
(a member, a canvas, a problem, an opportunity, an SRS document, a key of the signals, the Curator's charter)."""

from __future__ import annotations

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import goals, tables
from evo_agents.hub import opportunities as opportunity_model
from evo_agents.hub import problems as problem_model
from evo_agents.hub.server.base.errors import NotFound
from evo_agents.hub.server.base.revisions import Project, RevisionedStore


def _select():
    """A goal at a revision, with its owner's and writer's logins, its canvas's slug and its problem's number."""
    g, r, c, pr = tables.goals, tables.goal_revisions, tables.canvas.canvases, tables.problems
    op, sd = tables.opportunities, tables.srs.documents
    owner, writer = tables.users.alias("owner"), tables.users.alias("writer")
    return (
        select(
            g.c.id,
            g.c.number,
            g.c.alias,
            r.c.revision,
            r.c.statement,
            r.c.metric,
            r.c.deadline,
            r.c.canvas_id,
            c.c.slug.label("canvas"),
            r.c.problem_id,
            pr.c.number.label("problem_number"),
            r.c.opportunity_id,
            op.c.number.label("opportunity_number"),
            r.c.srs_document_id,
            sd.c.slug.label("srs_document"),
            r.c.srs_heading,
            r.c.rank,
            r.c.owner_id,
            owner.c.login.label("owner"),
            r.c.status,
            r.c.close_reason,
            r.c.note,
            writer.c.login.label("created_by"),
            r.c.created_at,
        )
        .join_from(g, r, r.c.goal_id == g.c.id)
        .join(owner, owner.c.id == r.c.owner_id)
        .join(writer, writer.c.id == r.c.created_by)
        .outerjoin(c, c.c.id == r.c.canvas_id)
        .outerjoin(pr, pr.c.id == r.c.problem_id)
        .outerjoin(op, op.c.id == r.c.opportunity_id)
        .outerjoin(sd, sd.c.id == r.c.srs_document_id)
    )


def _named(ref: str):
    g = tables.goals
    number = goals.number_of(ref)
    return g.c.number == number if number is not None else g.c.alias == ref


class GoalStore(RevisionedStore):
    lock_name = "goals"
    noun = "goal"
    head = tables.goals
    revisions = tables.goal_revisions
    key = "goal_id"

    def code(self, number: int) -> str:
        return goals.code(number)

    def number_of(self, ref: str) -> int | None:
        return goals.number_of(ref)

    async def found(self, conn: AsyncConnection, project_id: int, ref: str, revision: int | None = None):
        g, r = tables.goals, tables.goal_revisions
        at = g.c.revision if revision is None else revision
        query = _select().where(g.c.project_id == project_id, _named(ref), r.c.revision == at)
        return (await conn.execute(query)).first()

    async def number_of_new(self, conn: AsyncConnection, access: Project, ref: str) -> int:
        """The number a new goal named ``ref`` takes: the next one; 404 for an alias, 422 for another code."""
        if goals.number_of(ref) is None:
            following = await self.next_number(conn, access.project_id)
            raise NotFound(
                f"project {access.name} has no goal {ref}; a new goal takes the code {goals.code(following)}"
            )
        return await super().number_of_new(conn, access, ref)

    def stored(self, row) -> dict:
        return {
            "statement": row.statement,
            "metric": row.metric,
            "deadline": row.deadline,
            "canvas_id": row.canvas_id,
            "problem_id": row.problem_id,
            "opportunity_id": row.opportunity_id,
            "srs_document_id": row.srs_document_id,
            "srs_heading": row.srs_heading,
            "rank": row.rank,
            "owner_id": row.owner_id,
            "status": row.status,
            "close_reason": row.close_reason,
        }

    async def next_rank(self, conn: AsyncConnection, project_id: int) -> int:
        """After the project's goals that are not closed."""
        g, r = tables.goals, tables.goal_revisions
        query = (
            select(func.coalesce(func.max(r.c.rank), 0))
            .join_from(g, r, and_(r.c.goal_id == g.c.id, r.c.revision == g.c.revision))
            .where(g.c.project_id == project_id, r.c.status != goals.CLOSED)
        )
        return min(int(await conn.scalar(query) or 0) + 1, goals.MAX_RANK)

    async def listed(self, conn: AsyncConnection, project_id: int, status: str | None = None) -> list:
        """The project's goals at their newest revision, by rank then code; those of ``status`` alone when given."""
        g, r = tables.goals, tables.goal_revisions
        query = _select().where(g.c.project_id == project_id, r.c.revision == g.c.revision)
        if status is not None:
            query = query.where(r.c.status == status)
        return list((await conn.execute(query.order_by(r.c.rank, g.c.number))).all())

    async def revisions_of(self, conn: AsyncConnection, goal_id: int) -> list:
        """Every revision of goal ``goal_id``, the oldest first."""
        query = _select().where(tables.goals.c.id == goal_id).order_by(tables.goal_revisions.c.revision)
        return list((await conn.execute(query)).all())

    async def newest_charter(self, conn: AsyncConnection, project_id: int):
        """The revision, body and writer of the project's newest charter; None without one."""
        c = tables.charters
        query = select(c.c.revision, c.c.body, c.c.created_by).where(c.c.project_id == project_id)
        return (await conn.execute(query.order_by(c.c.revision.desc()).limit(1))).first()

    async def aliases_taken(self, conn: AsyncConnection, project_id: int, aliases: list[str]) -> set[str]:
        g = tables.goals
        query = select(g.c.alias).where(g.c.project_id == project_id, g.c.alias.in_(aliases))
        return set((await conn.execute(query)).scalars())


STORE = GoalStore()


async def member_role(conn: AsyncConnection, project_id: int, login: str):
    """The id of member ``login`` and the role they hold on the project (None without a grant); None for no one."""
    u, gr = tables.users, tables.grants
    query = (
        select(u.c.id, gr.c.role)
        .join_from(u, gr, and_(gr.c.user_id == u.c.id, gr.c.project_id == project_id), isouter=True)
        .where(func.lower(u.c.login) == login.lower())
    )
    return (await conn.execute(query)).first()


async def canvas(conn: AsyncConnection, project_id: int, slug: str):
    """The id of canvas ``slug`` of the project and when it was archived; None without one."""
    c = tables.canvas.canvases
    query = select(c.c.id, c.c.archived_at).where(c.c.project_id == project_id, c.c.slug == slug)
    return (await conn.execute(query)).first()


async def signal_key(conn: AsyncConnection, project_id: int, key: str):
    """The kind of key ``key`` of the project's registry of signals and when it was archived; None without one."""
    k = tables.signal_keys
    query = select(k.c.kind, k.c.archived_at).where(k.c.project_id == project_id, k.c.name == key)
    return (await conn.execute(query)).first()


async def problem(conn: AsyncConnection, project_id: int, ref: str):
    """The id of problem ``ref`` of the project and its status now; None without one."""
    p, r = tables.problems, tables.problem_revisions
    query = (
        select(p.c.id, r.c.status)
        .join_from(p, r, and_(r.c.problem_id == p.c.id, r.c.revision == p.c.revision))
        .where(p.c.project_id == project_id, p.c.number == problem_model.number_of(ref))
    )
    return (await conn.execute(query)).first()


async def opportunity(conn: AsyncConnection, project_id: int, ref: str):
    """The id of opportunity ``ref`` of the project and its status now; None without one."""
    o, r = tables.opportunities, tables.opportunity_revisions
    query = (
        select(o.c.id, r.c.status)
        .join_from(o, r, and_(r.c.opportunity_id == o.c.id, r.c.revision == o.c.revision))
        .where(o.c.project_id == project_id, o.c.number == opportunity_model.number_of(ref))
    )
    return (await conn.execute(query)).first()


async def srs_document(conn: AsyncConnection, project_id: int, slug: str):
    """The id of SRS document ``slug`` of the project and when it was archived; None without one."""
    d = tables.srs.documents
    query = select(d.c.id, d.c.archived_at).where(d.c.project_id == project_id, d.c.slug == slug)
    return (await conn.execute(query)).first()
