"""What a deliver's plan run merged on its forge itself (the tick's step 2, before ``reconcile``).

The hub's merge queue merges nothing on GitHub, nor on GitLab in a repo the autonomy policy does not have in mode auto,
so such a repo of a deliver's plan is one a person merges (``View.manual``, ``merge_manual``), done once the plan marks
it merged (``View.released``). A plan run's agent may merge the pull request of its branch itself, or one pull request
for each step, each from a branch of its own, and writes nothing in the plan's ``repos`` when it does. So for each
deliver not finished whose plan run is done (the newest run that goes on from the one handed out for it), each repo of
the run (``runs.repos``) that the plan does not mark merged or not needed is looked up:

- on GitHub, with the workers' App (a token of pull_requests read alone, ``pulls.SERVER_PERMISSIONS["pulls"]``): the
  pull requests of the run's branch (``pulls.branch_pulls``), then, when none of them settles it, the repo's pull
  requests updated since the deliver's run was queued (``pulls.updated_pulls``, at most ``pulls.UPDATED_PAGES`` pages);
- on GitLab, for a repo the policy does not have in mode auto, with the project's forge secret for the repo's origin
  (``forge.forge_token``): the project's merge requests updated since then (``GitLab.updated_merge_requests``, at most
  GITLAB_PAGES pages).

``settled`` says the repo merged when the pull request (or merge request) of the run's branch merged once the
deliver's run was queued, or else when the run's step pull requests did: those of the repo itself whose head is
``<branch>-<n>`` or ``<branch>-s<n>`` (STEP), opened or merged since then, at least one merged since then and none
still open. The newest one merged gives the repo's ``merged_at``; the plan's ``note`` names it with its merge commit,
and every step one. The repo is marked merged in the plan (``status`` merged, ``merged_at`` its UTC date, the
``note``), written as the member who dispatched the run through PATCH's own write (``plans.apply_patch``), as that
member would with ``evo-agents hub plan patch``. The engine then reads the repo released and the deliver goes to done
with no person.

A pull request a person merges by hand goes the same way. A repo with nothing merged since, a step pull request still
open, a branch the run does not name, a repo on another forge or one the merge queue merges, an App not installed on
the repo, an origin on GitLab no forge secret of the project covers, a forge failing or listing more than the hub
reads, or a plan the dispatcher can no longer write leaves the deliver waiting as before, and the next tick looks
again. The forges are called outside any transaction, so the project's row of loop_states is not held meanwhile.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.autonomy import AUTO
from evo_agents.hub.plans import PlanProblem
from evo_agents.hub.server import pulls
from evo_agents.hub.server.autonomy.service.policies import effective_policy
from evo_agents.hub.server.credentials.models.sealing import Sealer
from evo_agents.hub.server.forge.client.github_app import GitHubApp
from evo_agents.hub.server.forge.models.forges import forge_of, github_target
from evo_agents.hub.server.forge.models.github import GitHubRefused, GitHubUnavailable, Target
from evo_agents.hub.server.forge.models.gitlab import GitLabRefused, GitLabUnavailable, MergeRequest, project_of
from evo_agents.hub.server.forge.service.secrets import forge_token
from evo_agents.hub.server.loop.model import DELIVER, FINISHED
from evo_agents.hub.server.loop.view import RELEASED, run_chains
from evo_agents.hub.server.repo_origins import repo_origins

log = logging.getLogger(__name__)

MERGED = "merged"  # the status of a repo of a plan the run merged; also the state of a change merged
OPEN = "open"
CLOSED = "closed"  # closed without a merge
STEP = re.compile(r"s?\d+")  # what follows "<run branch>-" in the branch of one of the run's steps
GITLAB_PAGES = 3  # of a GitLab project's merge requests by update: 300 at most


@dataclass(frozen=True)
class Wanted:
    """A repo of a deliver's plan a person merges, which its plan does not mark merged, with the branch its plan run
    worked in."""

    item_id: int
    plan_id: str
    run_id: int  # the newest run of the deliver, which is done
    since: datetime  # when the run handed out for the deliver was queued: a pull request merged before is not its
    dispatcher_id: int
    dispatcher: str
    project: str
    repo: str
    branch: str
    target: Target | None  # owner/name on GitHub; None for a repo on GitLab
    origin: str


@dataclass(frozen=True)
class Change:
    """A pull request on GitHub or a merge request on GitLab of the repo itself, as ``settled`` reads it."""

    number: int  # GitHub's number, GitLab's iid
    branch: str  # its head
    state: str  # OPEN, MERGED or CLOSED
    created_at: datetime | None
    merged_at: datetime | None
    sha: str | None  # its merge commit


@dataclass(frozen=True)
class Merged:
    number: int
    merged_at: datetime
    sha: str | None
    steps: tuple[int, ...] = ()  # the step pull requests merged, in number order; empty for the run branch's own


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


def _looked_up(repo: str, origin: str, by_hand: frozenset[str]) -> bool:
    """Whether the Loop reads what ``repo`` at ``origin`` merged: on GitHub, or on GitLab in a mode a person merges."""
    forge = forge_of(origin)
    return forge is not None and forge.loop_reads(origin, repo, by_hand)


async def wanted(conn: AsyncConnection, project_id: int) -> list[Wanted]:
    """The repos to look up on their forge for the delivers of project ``project_id`` whose plan run is done."""
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
    effective = await effective_policy(conn, project_id)
    by_hand = frozenset(name for name, entry in effective["repos"].items() if entry.get("mode") != AUTO)
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
            origin = origins[repo].origin if repo in origins else None
            if not origin or not _looked_up(repo, origin, by_hand) or not isinstance(branch, str) or not branch:
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
                    github_target(origin),
                    origin,
                )
            )
    return found


def is_step(head: str, branch: str) -> bool:
    """Whether ``head`` is the branch of a step of a run on ``branch``: ``<branch>-<n>`` or ``<branch>-s<n>``."""
    prefix = f"{branch}-"
    return head.startswith(prefix) and STEP.fullmatch(head[len(prefix) :]) is not None


def _newest(changes: Iterable[Change], since: datetime) -> list[tuple[datetime, Change]]:
    """The changes of ``changes`` merged at or after ``since``, each with when, the newest first."""
    merged = [(change.merged_at, change) for change in changes if change.state == MERGED and change.merged_at]
    return sorted(((at, change) for at, change in merged if at >= since), key=lambda pair: pair[0], reverse=True)


def settled(changes: Sequence[Change], branch: str, since: datetime) -> Merged | None:
    """The newest merge of ``changes`` that says the run on ``branch`` merged since ``since`` (see the module): the
    change of ``branch`` itself, else those of its steps; None when they do not."""
    own = _newest((change for change in changes if change.branch == branch), since)
    if own:
        at, change = own[0]
        return Merged(change.number, at, change.sha)
    steps = [
        change
        for change in changes
        if is_step(change.branch, branch)
        and any(when is not None and when >= since for when in (change.created_at, change.merged_at))
    ]
    merged = _newest(steps, since)
    if not merged or any(change.state == OPEN for change in steps):
        return None
    at, change = merged[0]
    return Merged(change.number, at, change.sha, tuple(sorted(found.number for _, found in merged)))


def github_change(pull: dict, target: Target) -> Change | None:
    """``pull`` as GitHub lists it, unless its head is a fork's (as ``merge_check.pull_refusal`` reads it)."""
    head = pull.get("head")
    head = head if isinstance(head, dict) else {}
    ref, number, repo = head.get("ref"), pull.get("number"), head.get("repo")
    if not isinstance(ref, str) or type(number) is not int:
        return None
    if "repo" in head and not (
        isinstance(repo, dict) and str(repo.get("full_name") or "").lower() == f"{target.owner}/{target.name}".lower()
    ):
        return None  # a fork's, or one GitHub no longer has
    merged_at = _when(pull.get("merged_at"))
    state = MERGED if merged_at is not None else OPEN if pull.get("state") == "open" else CLOSED
    sha = pull.get("merge_commit_sha")
    return Change(number, ref, state, _when(pull.get("created_at")), merged_at, sha if isinstance(sha, str) else None)


def gitlab_change(mr: MergeRequest) -> Change | None:
    """``mr`` as GitLab lists it, when its source branch is of its project itself."""
    if mr.source_project_id not in (None, mr.project_id):
        return None  # a fork's
    state = MERGED if mr.state == "merged" else CLOSED if mr.state == "closed" else OPEN  # opened, locked
    return Change(mr.iid, mr.source_branch, state, _when(mr.created_at), _when(mr.merged_at), mr.merge_commit_sha)


def _github_changes(listed: list[dict], target: Target) -> list[Change]:
    return [change for pull in listed if (change := github_change(pull, target)) is not None]


async def merged_pull(app: GitHubApp, want: Wanted) -> Merged | None:
    """What GitHub says the run of ``want`` merged since its run was queued (``settled``); None when nothing, or
    GitHub cannot tell now."""
    target = want.target
    if target is None:
        return None

    async def read(token: str) -> Merged | None:
        own = await pulls.branch_pulls(app, token, target.owner, target.name, want.branch)
        found = settled(_github_changes(own, target), want.branch, want.since)
        if found is not None:
            return found
        listed = await pulls.updated_pulls(app, token, target.owner, target.name, want.since)
        return settled(_github_changes(listed, target), want.branch, want.since)

    where = {"item_id": want.item_id, "plan_id": want.plan_id, "repo": want.repo, "branch": want.branch}
    try:
        return await pulls.with_token(app, target.owner, target.name, "pulls", read)
    except GitHubRefused as exc:
        log.warning("the pull requests of a deliver's branch could not be read", extra={**where, "why": str(exc)})
        return None
    except GitHubUnavailable as exc:
        log.info("GitHub did not answer for a deliver's branch; the next tick asks", extra={**where, "why": str(exc)})
        return None


async def merged_requests(
    engine: AsyncEngine,
    sealer: Sealer,
    project_id: int,
    found: Sequence[Wanted],
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[tuple[Wanted, Merged]]:
    """What GitLab says the runs of ``found`` (repos on GitLab) merged since their runs were queued (``settled``), read
    with project ``project_id``'s forge secret for each origin; a repo no secret covers, or GitLab cannot tell now
    about, is left out."""
    by_origin: dict[str, list[Wanted]] = {}
    for want in found:
        by_origin.setdefault(want.origin, []).append(want)
    merged: list[tuple[Wanted, Merged]] = []
    for origin, wants in by_origin.items():
        async with engine.connect() as conn:
            token = await forge_token(conn, sealer, project_id, origin)
        if token is None:  # a person marks the repo, as before
            continue
        async with token.gitlab(transport=transport) as gl:
            for want in wants:
                where = {"item_id": want.item_id, "plan_id": want.plan_id, "repo": want.repo, "branch": want.branch}
                try:
                    listed = await gl.updated_merge_requests(token.project.path, want.since, pages=GITLAB_PAGES)
                except GitLabRefused as exc:
                    log.warning(
                        "the merge requests of a deliver's repo could not be read", extra={**where, "why": str(exc)}
                    )
                    continue
                except GitLabUnavailable as exc:
                    log.info(
                        "GitLab did not answer for a deliver's repo; the next tick asks",
                        extra={**where, "why": str(exc)},
                    )
                    continue
                changes = [change for mr in listed if (change := gitlab_change(mr)) is not None]
                if (pull := settled(changes, want.branch, want.since)) is not None:
                    merged.append((want, pull))
    return merged


def note(want: Wanted, pull: Merged) -> str:
    sha = f" ({pull.sha[:7]})" if pull.sha else ""
    at = pull.merged_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")
    if want.target is not None:
        forge, kind, name, mark = "GitHub", "PR", f"{want.target.owner}/{want.target.name}", "#"
    else:
        project = project_of(want.origin)
        forge, kind, name, mark = "GitLab", "MR", project.path if project is not None else want.repo, "!"
    read = f"read on {forge} by the Loop after plan run #{want.run_id}"
    if not pull.steps:
        return f"{kind} {name}{mark}{pull.number}{sha}, merged {at}; {read}"
    listed = ", ".join(f"{mark}{number}" for number in pull.steps)
    return (
        f"{kind}s {name}{listed} of the steps of {want.branch}, the last {mark}{pull.number}{sha} merged {at}; {read}"
    )


async def mark(conn: AsyncConnection, want: Wanted, pull: Merged) -> bool:
    """Mark ``want``'s repo merged in its plan as the run's dispatcher, in a savepoint of the caller's transaction;
    whether the plan changed. A write the plan refuses is logged and skipped."""
    from evo_agents.hub.server.plans import service as plan_service
    from evo_agents.hub.server.plans.models import PlanError
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
            held = await plan_service.held(conn, access, want.plan_id)
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
            _, changed, _ = await plan_service.apply_patch(
                conn, access, actor, want.plan_id, "repos", updates, if_revision=held.revision, index=index
            )
    except HTTPException as exc:
        log.warning("a merged repo not marked in its plan", extra={**where, "status": exc.status_code})
        return False
    except PlanError as exc:
        log.warning("a merged repo not marked in its plan", extra={**where, "status": exc.status, "why": exc.message})
        return False
    except PlanProblem as exc:
        log.warning("a merged repo not marked in its plan", extra={**where, "why": str(exc)})
        return False
    if changed:
        log.info("a repo a deliver's plan run merged marked merged in its plan", extra=where)
    return changed


async def settle(
    engine: AsyncEngine,
    project_id: int,
    app: GitHubApp | None,
    sealer: Sealer | None = None,
    gitlab_transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Mark merged in their plans the repos of project ``project_id``'s delivers whose plan runs merged (see the
    module); how many. GitHub is read with the workers' App ``app``, GitLab with the forge secrets ``sealer`` opens;
    nothing without either."""
    if app is None and sealer is None:
        return 0
    async with engine.connect() as conn:
        found: Sequence[Wanted] = await wanted(conn, project_id)
    merged: list[tuple[Wanted, Merged]] = []
    if app is not None:
        merged += [(want, pull) for want in found if want.target is not None and (pull := await merged_pull(app, want))]
    on_gitlab = [want for want in found if want.target is None]
    if sealer is not None and on_gitlab:
        merged += await merged_requests(engine, sealer, project_id, on_gitlab, gitlab_transport)
    if not merged:
        return 0
    async with engine.begin() as conn:
        return sum([await mark(conn, want, pull) for want, pull in merged])


__all__ = [
    "GITLAB_PAGES",
    "MERGED",
    "STEP",
    "Change",
    "Merged",
    "Wanted",
    "github_change",
    "gitlab_change",
    "is_step",
    "mark",
    "merged_pull",
    "merged_requests",
    "note",
    "settle",
    "settled",
    "wanted",
]
