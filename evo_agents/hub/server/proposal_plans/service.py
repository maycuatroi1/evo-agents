"""An accepted proposal of the Curator as its plan (``plan_from_proposal``), a stuck change planned again
(``replan_stuck``), and POST .../proposals/{id}/plan, for the project's admins (``plan_proposal``); the routes of
``evo_agents.hub.server.proposal_plans`` say when each happens."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import judge
from evo_agents.hub.access import has_role
from evo_agents.hub.server import ledger as ledger_lines
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict
from evo_agents.hub.server.base.scope import Noun, ProjectScope
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.changes.models.views import Change
from evo_agents.hub.server.changes.repository.repos import record_repos
from evo_agents.hub.server.changes.repository.views import change_view
from evo_agents.hub.server.changes.service import repos as change_repos
from evo_agents.hub.server.forge.models.forges import GITHUB
from evo_agents.hub.server.forge.models.protection import reason_line
from evo_agents.hub.server.forge.repository.protection import repo_origins
from evo_agents.hub.server.plans.models import PLAN_CREATE, PlanError
from evo_agents.hub.server.plans.service import schema_checked, store
from evo_agents.hub.server.probes.service import probes
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.proposal_plans.repository import STORE
from evo_agents.hub.server.proposals.service.reads import readable_proposal
from evo_agents.hub.server.proposals.service.record import planned_tiers_of
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)

PLAN = "curator.plan"  # an accepted proposal became a plan: "<project>/<plan> proposal:<id> change:<id> branch=<b>"


class CuratorAdmins(ProjectScope):
    """The project's admins, who alone make an accepted proposal the Curator's plan by hand."""

    def refusal(self, user: Principal, access: ProjectAccess) -> str | None:
        if has_role(access.role, "admin"):
            return None
        held = access.role or "no role"
        return (
            f"making a proposal of project {access.name} the Curator's plan needs the admin role on it; you hold {held}"
        )


ADMINS = CuratorAdmins("admin", Noun("a proposal's plan", "the proposals' plans"))


# An accepted proposal becomes a plan


async def _record(conn: AsyncConnection, access: ProjectAccess, user: Principal, proposal, values: dict) -> int:
    values = {"project_id": access.project_id, "proposal_id": proposal.id, "tier": proposal.tier, **values}
    change_id = await STORE.record(conn, values)
    plan = values.get("plan_id") or "no plan"
    branch = values.get("branch") or "-"
    target = f"{access.name}/{plan} proposal:{proposal.id} change:{change_id} branch={branch}"
    await Auditor.of(conn, user, access).record(PLAN, target)
    return change_id


def _named_repos(draft: dict, proposal, origins: dict) -> list[str]:
    """The repos ``proposal`` names, each once: those of its draft's steps and repos in the draft's order, then those of
    its paths; the project's one repo when it names none."""
    named = []
    for section in ("steps", "repos"):
        for item in draft.get(section) or []:
            if isinstance(item, dict) and isinstance(item.get("repo"), str):
                named.append(item["repo"])
    for item in proposal.paths or []:
        if isinstance(item, dict) and isinstance(item.get("repo"), str):
            named.append(item["repo"])
    if not named and len(origins) == 1:
        named = list(origins)
    return list(dict.fromkeys(named))


def _not_plannable(named: list[str], origins: dict) -> str | None:
    """Why a plan cannot work in the several repos ``named``: each must be a repo of the project on GitHub."""
    away = [repo for repo in named if repo not in origins or judge.forge_of(origins[repo]) != GITHUB.name]
    if not away:
        return None
    return (
        f"a Curator's plan works in one repo, and the proposal names {', '.join(sorted(named))}: a plan in several "
        f"repos works on GitHub alone, and {', '.join(away)} {'is' if len(away) == 1 else 'are'} not there"
    )


async def _plan_body(conn: AsyncConnection, access: ProjectAccess, proposal, draft: dict, named: list[str]):
    """(the plan's body, its id, its branch, the rows of its repos: None for a plan in one repo) of ``proposal``'s
    draft in the repos ``named``; ValueError for a step in none of them."""
    draft_id = str(draft.get("id") or proposal.title)
    plan_id = judge.curator_plan_id(proposal.id, draft_id)
    branch = judge.curator_branch(proposal.id, draft_id)
    if len(named) == 1:
        body = judge.curator_plan(
            draft, plan_id=plan_id, repo=named[0], branch=branch, proposal_id=proposal.id, project=access.name
        )
        return body, plan_id, branch, None
    seams = await change_repos.project_seams(conn, access.project_id, draft)
    body = judge.curator_repos_plan(
        draft,
        plan_id=plan_id,
        repos=named,
        branch=branch,
        proposal_id=proposal.id,
        project=access.name,
        seams=seams,
    )
    return body, plan_id, branch, change_repos.repo_rows([item["repo"] for item in body["repos"]], seams)


async def _make_plan(conn: AsyncConnection, access: ProjectAccess, user: Principal, proposal) -> dict | str:
    """The values of the change the draft of ``proposal`` becomes, its plan stored; or why it cannot become one. A
    change in several repos has the rows of its repos too ("rows", ``change_repos.repo_rows``), its own repo the
    first."""
    draft = proposal.plan if isinstance(proposal.plan, dict) else {}
    origins = await repo_origins(conn, access.project_id)
    named = _named_repos(draft, proposal, origins)
    if len(named) > 1 and (why := _not_plannable(named, origins)) is not None:
        return why
    if not named:
        return "a Curator's plan works in one repo, and the proposal names no repo"
    if len(named) == 1 and named[0] not in origins:
        return f"project {access.name} has no repo {named[0]}"
    forge = judge.forge_of(origins[named[0]])
    if forge is None:
        return f"repo {named[0]} has no origin a Builder could push a branch to"
    try:
        body, plan_id, branch, rows = await _plan_body(conn, access, proposal, draft, named)
        schema_checked(body, plan_id)
        label = access.push_label(proposal.label)
        async with conn.begin_nested():  # a store refused after its insert takes nothing with it
            stored = await store(
                conn,
                access,
                user,
                None,
                area="active",
                label=label,
                body=body,
                summary=f"made by the Curator from proposal #{proposal.id}",
                action=PLAN_CREATE,
                made_by_curator=True,
            )
    except ValueError as exc:
        return str(exc)
    except PlanError as exc:
        return exc.message
    except HTTPException as exc:
        return str(exc.detail)
    if stored is None:
        return f"project {access.name} has a plan {plan_id} already"
    repo = rows[0]["repo"] if rows else named[0]
    return {"plan_id": plan_id, "repo": repo, "branch": branch, "forge": forge, "rows": rows}


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
    rows = made.pop("rows")
    change_id = await _record(conn, access, user, proposal, made)
    if rows:
        await record_repos(conn, access.project_id, change_id, rows, made["branch"])
    await probes.attach(conn, proposal.id, change_id)
    await ledger_lines.change_recorded(conn, change_id)
    log.info("curator plan made", extra={"project": access.name, "plan_id": made["plan_id"], "change_id": change_id})
    return change_id


# A proposal accepted before it could become one


STUCK_STATES = ("open", "closed")  # the states of a change with no plan that may be planned again


def _stuck(found) -> bool:
    """Whether change ``found`` never got a plan, a branch or a pull request: one ``plan_from_proposal`` left open,
    or such a change closed by hand since."""
    return found.state in STUCK_STATES and found.plan_id is None and found.branch is None and found.pr_number is None


async def _unplanned(conn: AsyncConnection, project_id: int, proposal, found) -> str | None:
    """Why ``proposal`` does not become the Curator's plan now: it is not accepted, it promotes memories, its tier is
    not one the charter plans (tier 3 unless the charter names it), or it has a change ``found`` already that is not
    stuck (``_stuck``); None when it does."""
    if proposal.state != "accepted":
        return f"proposal {proposal.id} is {proposal.state}: only an accepted proposal becomes the Curator's plan"
    if proposal.promotion is not None:
        return f"proposal {proposal.id} promotes agents' memories: accepting it wrote them, and it becomes no plan"
    if proposal.system_change is not None:
        return (
            f"proposal {proposal.id} changes an agent system's target: accepting it applied it, and it becomes no plan"
        )
    planned = await planned_tiers_of(conn, project_id)
    if proposal.tier not in planned:
        return (
            f"proposal {proposal.id} is tier {proposal.tier}: the night shift builds {judge.tier_words(planned)} "
            f"alone, and its owner makes a change of tier {proposal.tier}"
        )
    if found is not None and not _stuck(found):
        return (
            f"proposal {proposal.id} has change #{found.id} already, {found.state}: `evo-agents hub curator changes` "
            "shows it"
        )
    return None


async def replan_stuck(conn: AsyncConnection, access: ProjectAccess, user: Principal, proposal, found) -> str | None:
    """Make the draft of ``proposal`` the Curator's plan in its stuck change ``found`` (``_stuck``), as ``user``: the
    row becomes ``planned`` with the plan, its reason cleared, audited as curator.plan and its ledger saying which
    state the plan replaced. Why it still cannot become one, with nothing changed; None once it is planned."""
    made = await _make_plan(conn, access, user, proposal)
    if isinstance(made, str):
        return made
    rows = made.pop("rows")
    await STORE.planned(conn, found.id, made)
    if rows:
        await record_repos(conn, access.project_id, found.id, rows, made["branch"])
    target = (
        f"{access.name}/{made['plan_id']} proposal:{proposal.id} change:{found.id} branch={made['branch']} "
        f"replaces={found.state}"
    )
    await Auditor.of(conn, user, access).record(PLAN, target)
    await probes.attach(conn, proposal.id, found.id)
    await ledger_lines.append(
        conn,
        project_id=access.project_id,
        proposal_id=proposal.id,
        change_id=found.id,
        action="planned",
        actor="curator",
        what=f"It became the Curator's plan {made['plan_id']} on {made['branch']}, replacing change #{found.id} "
        f"{found.state} with no plan: {found.reason or 'no reason recorded'}",
        details={
            "plan_id": made["plan_id"],
            "repo": made["repo"],
            "branch": made["branch"],
            "forge": made["forge"],
            "replaced": {"state": found.state, "reason": found.reason},
        },
    )
    log.info(
        "curator plan made again", extra={"project": access.name, "plan_id": made["plan_id"], "change_id": found.id}
    )
    return None


async def plan_proposal(engine: AsyncEngine, user: Principal, project: str, proposal_id: int) -> Change:
    """Make accepted proposal ``proposal_id`` that has no change yet, or a stuck one, the Curator's plan, as an admin
    of the project; the change, planned or left open with why. 409 with why for a proposal that does not become one
    (``_unplanned``), or whose stuck change still cannot."""
    async with UnitOfWork(engine) as conn:
        access = await ADMINS.check(conn, user, project)
        row = await readable_proposal(conn, access, proposal_id, None, user, lock=True)
        found = await STORE.change_of(conn, row.id)
        why = await _unplanned(conn, access.project_id, row, found)
        if why is not None:
            raise Conflict(f"{why}; nothing was made")
        if found is None:
            change_id = await plan_from_proposal(conn, access, user, row)
        elif (why := await replan_stuck(conn, access, user, row, found)) is not None:
            raise Conflict(
                f"proposal {row.id} still cannot become the Curator's plan: {why}; change #{found.id} stays {found.state}"
            )
        else:
            change_id = found.id
        view = await change_view(conn, change_id)
    log.info("proposal planned", extra={"proposal_id": proposal_id, "change_id": change_id, "login": user.login})
    return view
