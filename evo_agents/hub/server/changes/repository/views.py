"""Reading and writing a change of the Curator (``evo_agents.hub.server.changes.service.job`` describes the design):
the query of a change's view, a change by plan or by id, its repos, a write of it, the
charter of its project, its proposal's tier, and what a run of the Curator reads of its change, proposal and plan."""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import diff_signs, tables
from evo_agents.hub.server.changes.models.views import Change, ChangeRepo


def changes_query():
    c, p = tables.curator_changes, tables.projects
    return select(
        c.c.id,
        p.c.name.label("project"),
        c.c.proposal_id,
        c.c.plan_id,
        c.c.repo,
        c.c.branch,
        c.c.forge,
        c.c.tier,
        c.c.state,
        c.c.reason,
        c.c.builder_run_id,
        c.c.judge_run_id,
        c.c.pr_number,
        c.c.pr_url,
        c.c.base_branch,
        c.c.head_sha,
        c.c.passed,
        c.c.verdict,
        c.c.merged_at,
        c.c.merge_sha,
        c.c.created_at,
        c.c.updated_at,
    ).join_from(c, p, p.c.id == c.c.project_id)


async def change_of_plan(conn: AsyncConnection, project_id: int, plan_id: str | None, *, lock: bool = False):
    """The change whose plan is ``plan_id`` in the project, or None: a plan is the Curator's when it has one."""
    if not plan_id:
        return None
    c = tables.curator_changes
    query = select(c).where(c.c.project_id == project_id, c.c.plan_id == plan_id)
    if lock:
        query = query.with_for_update()
    return (await conn.execute(query)).one_or_none()


async def change_with_origin(conn: AsyncConnection, project_id: int, change_id: int, *, lock: bool = False):
    """Change ``change_id`` of the project with its repo's origin, as the job reads a change it moves on, or None."""
    c, pr = tables.curator_changes, tables.project_repos
    query = (
        select(c, pr.c.origin)
        .join_from(c, pr, (pr.c.project_id == c.c.project_id) & (pr.c.name == c.c.repo), isouter=True)
        .where(c.c.id == change_id, c.c.project_id == project_id)
    )
    if lock:
        query = query.with_for_update(of=c)
    return (await conn.execute(query)).one_or_none()


async def change_row(conn: AsyncConnection, change_id: int):
    """The row of curator_changes of change ``change_id``."""
    return (await conn.execute(select(tables.curator_changes).where(tables.curator_changes.c.id == change_id))).one()


async def change_view(conn: AsyncConnection, change_id: int) -> Change:
    row = (await conn.execute(changes_query().where(tables.curator_changes.c.id == change_id))).one()
    return (await with_repos(conn, [Change(**row._mapping)]))[0]


async def with_repos(conn: AsyncConnection, changes: list[Change]) -> list[Change]:
    """``changes`` with the repos of each that works in several, read at once."""
    cr = tables.curator_change_repos
    if not changes:
        return changes
    query = select(cr).where(cr.c.change_id.in_([change.id for change in changes])).order_by(cr.c.position)
    found: dict[int, list[ChangeRepo]] = {}
    for row in (await conn.execute(query)).all():
        found.setdefault(row.change_id, []).append(ChangeRepo.model_validate(row, from_attributes=True))
    for change in changes:
        change.repos = found.get(change.id, [])
    return changes


async def update_change(conn: AsyncConnection, change_id: int, values: dict) -> str | None:
    """Set ``values`` on change ``change_id``; the state it had, its row locked, when ``values`` sets one."""
    c = tables.curator_changes
    old = None
    if "state" in values:
        old = (await conn.execute(select(c.c.state).where(c.c.id == change_id).with_for_update())).scalar_one()
    await conn.execute(update(c).values(**values, updated_at=func.now()).where(c.c.id == change_id))
    return old


async def charter_of(conn: AsyncConnection, project_id: int) -> dict:
    c = tables.charters
    query = select(c.c.body).where(c.c.project_id == project_id).order_by(c.c.revision.desc()).limit(1)
    return (await conn.execute(query)).scalar_one_or_none() or {}


async def accepted_paths(conn: AsyncConnection, proposal_id: int):
    """The state, tier and paths of proposal ``proposal_id``, or None."""
    p = tables.proposals
    return (await conn.execute(select(p.c.state, p.c.tier, p.c.paths).where(p.c.id == proposal_id))).one_or_none()


async def project_repo_names(conn: AsyncConnection, project_id: int) -> list[str]:
    """The names of the project's repos, in order."""
    pr = tables.project_repos
    return list(
        (await conn.execute(select(pr.c.name).where(pr.c.project_id == project_id).order_by(pr.c.name))).scalars()
    )


async def store_judge_key(conn: AsyncConnection, change_id: int, digest: str) -> None:
    c = tables.curator_changes
    await conn.execute(update(c).values(judge_key=digest).where(c.c.id == change_id))


async def run_project_id(conn: AsyncConnection, run_id: int) -> int:
    r = tables.runs
    return (await conn.execute(select(r.c.project_id).where(r.c.id == run_id))).scalar_one()


async def judged_proposal(conn: AsyncConnection, proposal_id: int):
    """The id, title, kind, tier and summary of proposal ``proposal_id``, as a judge run's prompt names them."""
    pr = tables.proposals
    return (
        await conn.execute(
            select(pr.c.id, pr.c.title, pr.c.kind, pr.c.tier, pr.c.summary).where(pr.c.id == proposal_id)
        )
    ).one()


async def plan_revision_body(conn: AsyncConnection, project_id: int, plan_id: str, revision: int) -> dict:
    """The body of revision ``revision`` of plan ``plan_id``, {} without one."""
    rev = tables.plan_revisions
    query = select(rev.c.body).where(
        rev.c.project_id == project_id, rev.c.plan_id == plan_id, rev.c.revision == revision
    )
    return (await conn.execute(query)).scalar_one_or_none() or {}


async def plan_body(conn: AsyncConnection, project_id: int, plan_id: str | None) -> dict:
    """The body of plan ``plan_id`` as it is now, {} without one."""
    pl = tables.plans
    query = select(pl.c.body).where(pl.c.project_id == project_id, pl.c.plan_id == plan_id)
    return (await conn.execute(query)).scalar_one_or_none() or {}


async def raise_tier(conn: AsyncConnection, change, signs: list[dict]) -> None:
    """Put the change's proposal at tier 3, with the reason, once a diff of it shows signs."""
    pr = tables.proposals
    reasons = (await conn.execute(select(pr.c.tier_reasons).where(pr.c.id == change.proposal_id))).scalar_one()
    reason = diff_signs.raised_reason(signs)
    kept = list(reasons or [])
    if reason not in kept:
        kept.append(reason)
    await conn.execute(update(pr).values(tier=3, tier_reasons=kept).where(pr.c.id == change.proposal_id))
