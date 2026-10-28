"""What a deliver's plan run merged on GitHub itself (the tick's step 2, before ``reconcile``).

The hub's merge queue merges nothing on GitHub, so a repo of a deliver's plan on GitHub is one a person merges
(``View.manual``, ``merge_manual``), done once the plan marks it merged (``View.released``). A plan run's agent may merge
the pull request of its branch itself, and writes nothing in the plan's ``repos`` when it does. So for each deliver not
finished whose plan run is done (the newest run that goes on from the one handed out for it), each repo of the run
(``runs.repos``) on GitHub that the plan does not mark merged or not needed is looked up on GitHub with the workers'
App (a token of pull_requests read alone, ``pulls.SERVER_PERMISSIONS["pulls"]``): the pull request of the run's branch
merged once the deliver's run was queued marks the repo merged in the plan (``status`` merged, ``merged_at`` its UTC
date, a ``note`` naming the pull request and its merge commit), written as the member who dispatched the run through
PATCH's own write (``plans.apply_patch``), as that member would with ``evo-agents hub plan patch``. The engine then
reads the repo released and the deliver goes to done with no person.

A pull request a person merges by hand on GitHub goes the same way. A repo with no pull request merged since, a branch
the run does not name, a repo on another forge, an App not installed on the repo, GitHub failing, or a plan the
dispatcher can no longer write leaves the deliver waiting as before, and the next tick looks again. GitHub is called
outside any transaction, so the project's row of loop_states is not held meanwhile.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.plans import PlanProblem
from evo_agents.hub.server import pulls
from evo_agents.hub.server.github import GitHubRefused, GitHubUnavailable
from evo_agents.hub.server.github_app import GitHubApp
from evo_agents.hub.server.loop.model import DELIVER, FINISHED
from evo_agents.hub.server.loop.view import RELEASED, run_chains
from evo_agents.hub.server.protection import Target, github_target
from evo_agents.hub.server.repo_origins import repo_origins

log = logging.getLogger(__name__)

MERGED = "merged"  # the status of a repo of a plan the pull request of its branch merged


@dataclass(frozen=True)
class Wanted:
    """A repo of a deliver's plan on GitHub its plan does not mark merged, with the branch its plan run worked in."""

    item_id: int
    plan_id: str
    run_id: int  # the newest run of the deliver, which is done
    since: datetime  # when the run handed out for the deliver was queued: a pull request merged before is not its
    dispatcher_id: int
    dispatcher: str
    project: str
    repo: str
    branch: str
    target: Target


@dataclass(frozen=True)
class Merged:
    number: int
    merged_at: datetime
    sha: str | None


def _when(text) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")) if text else None
    except ValueError:
        return None


def _released(body) -> set[str]:
    entries = body.get("repos") if isinstance(body, dict) and isinstance(body.get("repos"), list) else []
    return {
        entry["repo"]
        for entry in entries
        if isinstance(entry, dict) and entry.get("repo") and entry.get("status") in RELEASED
    }


async def wanted(conn: AsyncConnection, project_id: int) -> list[Wanted]:
    """The repos to look up on GitHub for the delivers of project ``project_id`` whose plan run is done."""
    i, r, p, u, pj = tables.loop_items, tables.runs, tables.plans, tables.users, tables.projects
    query = select(i.c.id, i.c.plan_id, i.c.run_id).where(
        i.c.project_id == project_id,
        i.c.kind == DELIVER,
        i.c.state.not_in(FINISHED),
        i.c.plan_id.is_not(None),
        i.c.run_id.is_not(None),
    )
    items = list(await conn.execute(query.order_by(i.c.id)))
    if not items:
        return []
    chains = await run_chains(conn, [item.run_id for item in items])
    newest = {item.run_id: chains[item.run_id][-1] for item in items}
    ids = sorted({*newest.values(), *newest})
    query = select(r.c.id, r.c.kind, r.c.state, r.c.repos, r.c.queued_at, r.c.dispatched_by, u.c.login).join(
        u, u.c.id == r.c.dispatched_by
    )
    runs = {row.id: row for row in await conn.execute(query.where(r.c.id.in_(ids)))}
    plans = sorted({item.plan_id for item in items})
    query = select(p.c.plan_id, p.c.body).where(p.c.project_id == project_id, p.c.plan_id.in_(plans))
    released = {plan_id: _released(body) for plan_id, body in await conn.execute(query)}
    project = await conn.scalar(select(pj.c.name).where(pj.c.id == project_id))
    found: list[Wanted] = []
    for item in items:
        first, run = runs.get(item.run_id), runs.get(newest[item.run_id])
        if first is None or run is None or run.kind != "plan" or run.state != "done" or item.plan_id not in released:
            continue
        branches = {
            entry["repo"]: entry.get("branch")
            for entry in run.repos or []
            if isinstance(entry, dict) and entry.get("repo") and entry["repo"] not in released[item.plan_id]
        }
        origins = await repo_origins(conn, project_id, run.dispatched_by, branches)
        for repo, branch in branches.items():
            target = github_target(origins[repo].origin) if repo in origins else None
            if target is None or not isinstance(branch, str) or not branch:
                continue
            found.append(
                Wanted(
                    item.id,
                    item.plan_id,
                    run.id,
                    first.queued_at,
                    run.dispatched_by,
                    run.login,
                    project,
                    repo,
                    branch,
                    target,
                )
            )
    return found


async def merged_pull(app: GitHubApp, want: Wanted) -> Merged | None:
    """The newest pull request of ``want``'s branch merged since its run was queued; None when there is none, or
    GitHub cannot tell now."""

    async def read(token: str) -> list[dict]:
        return await pulls.branch_pulls(app, token, want.target.owner, want.target.name, want.branch)

    where = {"item_id": want.item_id, "plan_id": want.plan_id, "repo": want.repo, "branch": want.branch}
    try:
        listed = await pulls.with_token(app, want.target.owner, want.target.name, "pulls", read)
    except GitHubRefused as exc:
        log.warning("the pull requests of a deliver's branch could not be read", extra={**where, "why": str(exc)})
        return None
    except GitHubUnavailable as exc:
        log.info("GitHub did not answer for a deliver's branch; the next tick asks", extra={**where, "why": str(exc)})
        return None
    for pull in listed:  # newest first
        at = _when(pull.get("merged_at"))
        if at is not None and at >= want.since and type(pull.get("number")) is int:
            sha = pull.get("merge_commit_sha")
            return Merged(pull["number"], at, sha if isinstance(sha, str) and sha else None)
    return None


def note(want: Wanted, pull: Merged) -> str:
    sha = f" ({pull.sha[:7]})" if pull.sha else ""
    at = pull.merged_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")
    return (
        f"PR {want.target.owner}/{want.target.name}#{pull.number}{sha}, merged {at}; "
        f"read on GitHub by the Loop after plan run #{want.run_id}"
    )


async def mark(conn: AsyncConnection, want: Wanted, pull: Merged) -> bool:
    """Mark ``want``'s repo merged in its plan as the run's dispatcher, in a savepoint of the caller's transaction;
    whether the plan changed. A write the plan refuses is logged and skipped."""
    from evo_agents.hub.server import plans as plan_routes
    from evo_agents.hub.server.projects import project_access
    from evo_agents.hub.server.security import MACHINE, Principal

    actor = Principal(want.dispatcher_id, want.dispatcher, False, None, MACHINE, "")
    where = {"item_id": want.item_id, "plan_id": want.plan_id, "repo": want.repo, "pr": pull.number}
    try:
        async with conn.begin_nested():  # a savepoint: a refused write leaves the others in place
            access = await project_access(conn, actor, want.project)
            if not has_role(access.role, "writer"):
                log.warning("a merged repo not marked in its plan: the dispatcher is no writer", extra=where)
                return False
            held = await plan_routes._held(conn, access, want.plan_id)
            if held is None:
                return False
            entries = held.body.get("repos") if isinstance(held.body.get("repos"), list) else []
            index = next(
                (n for n, entry in enumerate(entries) if isinstance(entry, dict) and entry.get("repo") == want.repo),
                None,
            )
            if index is None or entries[index].get("status") in RELEASED:
                return False
            updates = {"status": MERGED, "merged_at": pull.merged_at.astimezone(UTC).date().isoformat()}
            updates["note"] = note(want, pull)
            _, changed, _ = await plan_routes.apply_patch(
                conn, access, actor, want.plan_id, "repos", updates, if_revision=held.revision, index=index
            )
    except HTTPException as exc:
        log.warning("a merged repo not marked in its plan", extra={**where, "status": exc.status_code})
        return False
    except plan_routes.PlanError as exc:
        log.warning("a merged repo not marked in its plan", extra={**where, "status": exc.status, "why": exc.message})
        return False
    except PlanProblem as exc:
        log.warning("a merged repo not marked in its plan", extra={**where, "why": str(exc)})
        return False
    if changed:
        log.info("a repo a deliver's plan run merged on GitHub marked merged in its plan", extra=where)
    return changed


async def settle(engine: AsyncEngine, project_id: int, app: GitHubApp | None) -> int:
    """Mark merged in their plans the repos of project ``project_id``'s delivers that GitHub says the pull requests of
    their plan runs' branches merged; how many. Nothing without the workers' App."""
    if app is None:
        return 0
    async with engine.connect() as conn:
        found: Sequence[Wanted] = await wanted(conn, project_id)
    merged = [(want, pull) for want in found if (pull := await merged_pull(app, want)) is not None]
    if not merged:
        return 0
    async with engine.begin() as conn:
        return sum([await mark(conn, want, pull) for want, pull in merged])


__all__ = ["MERGED", "Merged", "Wanted", "mark", "merged_pull", "note", "settle", "wanted"]
