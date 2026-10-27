"""``loop adopt``: a plan that is on the hub becomes a deliver of the Loop (plan srs-backlog-loop, criterion a6).

An admin of the project, or the writer who owns the goal, names a plan, a goal that aims at an SRS document and the
codes of the use cases the plan delivers. The deliver is a work item of kind ``deliver`` with no build above it
(``parent_id`` NULL), key ``deliver:<plan>``, ``plan_id`` the plan, the goal, the problem or opportunity of the goal and
the document and use cases as its ancestors; it goes through admission, the caps and the merge queue as the deliver of a
build does. In the same transaction the use cases are ``delivering`` in the ledger (``srs_ledger.hold``), so no build
takes them. A plan already completed has nothing to run: the deliver is made ``done`` and the use cases ``delivered`` at
once, with the plan and its merge requests as evidence (``srs_ledger.sync``).
"""

from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit
from evo_agents.hub.server.loop import backlog, baseline, delivery, srs_ledger, tick
from evo_agents.hub.server.loop.model import DELIVER, DONE, ENGINE, QUEUED, WorkSpec
from evo_agents.hub.server.loop.runs_of import goal_owner
from evo_agents.hub.srs_backlog import DELIVERING, ref_key

ADOPT = "loop.adopt"  # a plan adopted as a deliver: "<project>/goal G-<n> plan=<plan> use_cases=<count>"
MAX_CODES = 200


class LoopAdopt(BaseModel):
    plan: str = Field(description="the id of a plan of the project, active or completed")
    goal: str = Field(description="the code or alias of a goal that aims at an SRS document")
    use_cases: list[str] = Field(min_length=1, max_length=MAX_CODES, description="the codes the plan delivers")


class LoopAdopted(BaseModel):
    project: str
    goal: str
    plan: str
    item: int = Field(description="the id of the deliver")
    state: str
    reason: str | None
    use_cases: list[str]
    delivered: bool = Field(description="the plan was completed: the use cases are delivered now")


def allowed(access, user, owner: int | None) -> bool:
    """An admin of the project, or the writer who owns the goal; never the agent of a run."""
    if user.acts_for_run is not None:
        return False
    return has_role(access.role, "admin") or (has_role(access.role, "writer") and owner == user.user_id)


async def adopt(conn: AsyncConnection, user, access, body: LoopAdopt) -> LoopAdopted:
    goal_id, code, (_, document_id, _) = await baseline.goal_of(conn, access.project_id, body.goal)
    if not allowed(access, user, await goal_owner(conn, goal_id)):
        raise HTTPException(
            403,
            f"adopting a plan for goal {code} needs the admin role on project {access.name}, or its owner as writer",
        )
    p = tables.plans
    query = select(p.c.area).where(p.c.project_id == access.project_id, p.c.plan_id == body.plan)
    held = (await conn.execute(query)).first()
    if held is None:
        raise HTTPException(404, f"project {access.name} has no plan {body.plan}")
    known = {ref_key(case.ref): case for case in await backlog.use_cases(conn, access.project_id, document_id)}
    ledger = await delivery.read(conn, document_id)
    codes: list[str] = []
    for asked in body.use_cases:
        case = known.get(ref_key(asked))
        if case is None:
            raise HTTPException(422, f"goal {code} has no approved use case {asked} in its document")
        row = ledger.get(ref_key(asked))
        if row is not None and row.state == DELIVERING:
            raise HTTPException(409, f"use case {case.ref} is being delivered already (work item {row.item_id})")
        if case.ref not in codes:
            codes.append(case.ref)
    g, r = tables.goals, tables.goal_revisions
    ancestors = (
        await conn.execute(
            select(r.c.problem_id, r.c.opportunity_id)
            .join_from(g, r, (r.c.goal_id == g.c.id) & (r.c.revision == g.c.revision))
            .where(g.c.id == goal_id)
        )
    ).first()
    finished = held.area == "completed"
    spec = WorkSpec(
        DELIVER,
        f"deliver:{body.plan}",
        goal_id,
        DONE if finished else QUEUED,
        None,
        srs_document_id=document_id,
        plan_id=body.plan,
        detail={"adopted": {"use_cases": codes, "by": user.login}},
        problem_id=ancestors.problem_id if ancestors else None,
        opportunity_id=ancestors.opportunity_id if ancestors else None,
    )
    if not await tick.make(conn, access.project_id, spec, None, ENGINE):
        raise HTTPException(409, f"plan {body.plan} is a deliver of the Loop already")
    i = tables.loop_items
    query = select(i.c.id, i.c.state, i.c.reason).where(i.c.project_id == access.project_id, i.c.key == spec.key)
    item = (await conn.execute(query)).one()
    await srs_ledger.hold(conn, access.project_id, document_id, codes, item.id, goal_id, {"plan": body.plan})
    if finished:
        await srs_ledger.sync(conn, access.project_id)
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=ADOPT,
        target=f"{access.name}/goal {code} plan={body.plan} use_cases={len(codes)}",
        project_id=access.project_id,
    )
    return LoopAdopted(
        project=access.name,
        goal=code,
        plan=body.plan,
        item=item.id,
        state=item.state,
        reason=item.reason,
        use_cases=codes,
        delivered=finished,
    )


__all__ = ["ADOPT", "LoopAdopt", "LoopAdopted", "adopt"]
