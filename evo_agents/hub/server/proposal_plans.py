"""An accepted proposal of the Curator as its plan. ``evo_agents.hub.server.changes`` moves the change on from there:
the night shift's Builder, the pull request, the Judge, and the merge of a tier 0 change alone.

Accepting a proposal of tier 0 to 2 (``judge.PLANNED_TIERS``; ``proposals.answer_proposal_as``, on the web, the command
line or Telegram) makes its draft a plan on the hub in the same transaction (``plan_from_proposal``), as the admin who
accepted it, labelled as the proposal: the Curator's plan ``curator-<proposal>-<slug>``, in the one repo the proposal
names, on the branch ``curator/<proposal>-<slug>``, and a row of curator_changes, state ``planned``, with the line
``planned`` in the proposal's ledger. A proposal that names several repos, a repo the project does not have or one
without an origin cannot become one: its change is recorded ``open``, without a plan, with the reason and the line
``left_open``, and nothing of it runs. Either way the change is audited as curator.plan. Accepting a proposal of tier 3
makes nothing: its owner alone makes the change.

POST /v1/projects/{p}/curator/proposals/{id}/plan does the same for a proposal accepted without becoming a plan, such as
one of tier 2 accepted while that tier still recorded the answer alone: for the project's admins (403 otherwise), on an
accepted proposal of tier 0 to 2 that has no change yet (409 with why for one that is not accepted, is tier 3 or has a
change already). It answers the change, planned or left open with the reason.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import judge, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit, probes
from evo_agents.hub.server import ledger as ledger_lines
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.changes import Change, change_view
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.proposals import ItemId, readable_proposal
from evo_agents.hub.server.protection import reason_line, repo_origins
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

PLAN = "curator.plan"  # an accepted proposal became a plan: "<project>/<plan> proposal:<id> change:<id> branch=<b>"

router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409)}


# An accepted proposal becomes a plan


async def _record(conn: AsyncConnection, access: ProjectAccess, user: Principal, proposal, values: dict) -> int:
    c = tables.curator_changes
    values = {"project_id": access.project_id, "proposal_id": proposal.id, "tier": proposal.tier, **values}
    change_id = (await conn.execute(insert(c).values(**values).returning(c.c.id))).scalar_one()
    plan = values.get("plan_id") or "no plan"
    branch = values.get("branch") or "-"
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=PLAN,
        target=f"{access.name}/{plan} proposal:{proposal.id} change:{change_id} branch={branch}",
        project_id=access.project_id,
    )
    return change_id


async def _make_plan(conn: AsyncConnection, access: ProjectAccess, user: Principal, proposal) -> dict | str:
    """The values of the change the draft of ``proposal`` becomes, its plan stored; or why it cannot become one."""
    draft = proposal.plan if isinstance(proposal.plan, dict) else {}
    origins = await repo_origins(conn, access.project_id)
    named = {
        item["repo"] for item in proposal.paths or [] if isinstance(item, dict) and isinstance(item.get("repo"), str)
    }
    for section in ("steps", "repos"):
        for item in draft.get(section) or []:
            if isinstance(item, dict) and isinstance(item.get("repo"), str):
                named.add(item["repo"])
    if not named and len(origins) == 1:
        named = set(origins)
    if len(named) != 1:
        return f"a Curator's plan works in one repo, and the proposal names {', '.join(sorted(named)) or 'no repo'}"
    (repo,) = named
    if repo not in origins:
        return f"project {access.name} has no repo {repo}"
    forge = judge.forge_of(origins[repo])
    if forge is None:
        return f"repo {repo} has no origin a Builder could push a branch to"
    draft_id = str(draft.get("id") or proposal.title)
    plan_id = judge.curator_plan_id(proposal.id, draft_id)
    branch = judge.curator_branch(proposal.id, draft_id)
    body = judge.curator_plan(
        draft, plan_id=plan_id, repo=repo, branch=branch, proposal_id=proposal.id, project=access.name
    )
    try:
        plan_routes._schema_checked(body, plan_id)
        label = access.push_label(proposal.label)
        async with conn.begin_nested():  # a store refused after its insert takes nothing with it
            stored = await plan_routes._store(
                conn,
                access,
                user,
                None,
                area="active",
                label=label,
                body=body,
                summary=f"made by the Curator from proposal #{proposal.id}",
                action=plan_routes.PLAN_CREATE,
                made_by_curator=True,
            )
    except plan_routes.PlanError as exc:
        return exc.message
    except HTTPException as exc:
        return str(exc.detail)
    if stored is None:
        return f"project {access.name} has a plan {plan_id} already"
    return {"plan_id": plan_id, "repo": repo, "branch": branch, "forge": forge}


async def plan_from_proposal(conn: AsyncConnection, access: ProjectAccess, user: Principal, proposal) -> int:
    """Make the draft of ``proposal`` (a row of proposals, as ``proposals.readable_proposal`` reads it) the Curator's
    plan, as ``user``, and record its change; its id. A draft that cannot become one (see the module's docstring) is
    recorded as a change left open, with why, and no plan: nothing of it runs."""
    made = await _make_plan(conn, access, user, proposal)
    if isinstance(made, str):
        why = reason_line(f"it cannot become the Curator's plan: {made}")
        change_id = await _record(conn, access, user, proposal, {"state": "open", "reason": why})
        await probes.attach(conn, proposal.id, change_id)
        await ledger_lines.change_recorded(conn, change_id)
        log.info("curator plan not made", extra={"project": access.name, "proposal_id": proposal.id, "why": made})
        return change_id
    change_id = await _record(conn, access, user, proposal, made)
    await probes.attach(conn, proposal.id, change_id)
    await ledger_lines.change_recorded(conn, change_id)
    log.info("curator plan made", extra={"project": access.name, "plan_id": made["plan_id"], "change_id": change_id})
    return change_id


# A proposal accepted before it could become one


async def _refusal(conn: AsyncConnection, proposal) -> str | None:
    """Why ``proposal`` does not become the Curator's plan now: it is not accepted, it promotes memories, it is tier
    3, or it has a change already; None when it does."""
    if proposal.state != "accepted":
        return f"proposal {proposal.id} is {proposal.state}: only an accepted proposal becomes the Curator's plan"
    if proposal.promotion is not None:
        return f"proposal {proposal.id} promotes agents' memories: accepting it wrote them, and it becomes no plan"
    if proposal.tier not in judge.PLANNED_TIERS:
        return (
            f"proposal {proposal.id} is tier {proposal.tier}: the night shift builds tiers {judge.PLANNED_TIERS[0]} "
            f"to {judge.PLANNED_TIERS[-1]} alone, and its owner makes a change of tier {proposal.tier}"
        )
    c = tables.curator_changes
    found = (await conn.execute(select(c.c.id, c.c.state).where(c.c.proposal_id == proposal.id))).one_or_none()
    if found is not None:
        return (
            f"proposal {proposal.id} has change #{found.id} already, {found.state}: `evo-agents hub curator changes` "
            "shows it"
        )
    return None


@router.post(
    "/{project}/curator/proposals/{proposal_id}/plan", status_code=201, response_model=Change, responses=REFUSALS
)
async def plan_proposal(request: Request, project: ProjectName, proposal_id: ItemId, user: CurrentUser) -> Change:
    """Make an accepted proposal of tier 0 to 2 that has no change yet the Curator's plan, as accepting it does now;
    the project's admins alone may. The change: planned, or left open with why its draft cannot become a plan."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        if not has_role(access.role, "admin"):
            raise HTTPException(
                403,
                f"making a proposal of project {access.name} the Curator's plan needs the admin role on it; you hold "
                f"{access.role or 'no role'}",
            )
        row = await readable_proposal(conn, access, proposal_id, None, user, lock=True)
        why = await _refusal(conn, row)
        if why is not None:
            raise HTTPException(409, f"{why}; nothing was made")
        change_id = await plan_from_proposal(conn, access, user, row)
        view = await change_view(conn, change_id)
    log.info("proposal planned", extra={"proposal_id": proposal_id, "change_id": change_id, "login": user.login})
    return view
