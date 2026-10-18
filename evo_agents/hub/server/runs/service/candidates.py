"""Who can run a failed run again (GET /v1/projects/{p}/runs/{id}/rerun-candidates): each worker of the run's owner,
with whether it is fit and why, and each agent profile of the project that can be asked to do it, by fit. The hub
computes it from its rows, with no model: the worker rules are the ones a chat run goes by (``service.worker_fit``)
and the run's own needs on top of them, a checkout of each of its repos and an account its owner may use there.

A worker is fit when it is online, not draining, takes the run's kind, takes the way it was dispatched, has a free slot
(the runs queued for it count), has a checkout of every repo of the run, has a runtime and a model of the run's
profile, is one the profile allows, and, when its owner bound any account to the project, has one ready there (not
expired, paused or cooling). The fit ones are ranked by what the run failed of (``failure_cause``): the worker of the
run first for a missing program, checkout or branch, last for a worker that stopped, a lost lease or a runtime; a
worker with an account other than the run's first for a usage limit or an account that does not log in; then the one
with the most checkouts of the run's repos, the most free slots, the smallest id. An agent profile is fit when its
owner is a writer in the project, it was not deleted, a worker is fit for it, and for a missing program it lets the
agent run shell commands; the fit ones come first, the project's default among them, then by handle."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import profiles, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.accounts import account_state
from evo_agents.hub.server.profile_store import Profile, profile_of, profile_select
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.runs.service.controls import owned_run
from evo_agents.hub.server.runs.service.worker_fit import usable_runtimes, worker_problems, workers_of
from evo_agents.hub.server.security import Principal

OLD_FIRST = ("missing_tool", "checkout", "branch_mismatch")  # the worker of the run first: what is wrong is there
OLD_LAST = ("worker_stopped", "lease_lost", "runtime")  # the worker of the run last: it is what went wrong
NEW_ACCOUNT_FIRST = ("rate_limit", "account_auth")  # a worker with an account the run did not use first


class WorkerCandidate(BaseModel):
    worker_id: int
    worker: str
    fit: bool
    rank: int | None = Field(description="1 for the best fit worker; null for one that is no fit")
    reasons: list[str] = Field(description="what makes it fit, or each thing that makes it not")
    is_run_worker: bool = Field(description="the worker the failed run was on")
    online: bool
    free_slots: int = Field(description="slots left, the runs queued for the worker counted")
    runtimes: list[str] = Field(description="the runtimes it reports available that the run's profile allows")
    models: list[str] = Field(description="the models those runtimes list on the machine")
    labels: list[str]
    accounts: list[str] = Field(description="the accounts bound to the project that are ready on it")


class AgentCandidate(BaseModel):
    profile_id: int
    handle: str
    name: str
    is_default: bool
    fit: bool
    rank: int | None = Field(description="1 for the best fit agent; null for one that is no fit")
    reasons: list[str]


class AccountCandidate(BaseModel):
    name: str
    state: str = Field(description="ready, cooling, paused or expired")
    workers: list[str] = Field(description="the workers it is bound to for the project; any for every worker")
    used_by_run: bool


class RerunCandidates(BaseModel):
    run_id: int
    project: str
    kind: str
    state: str
    failure_cause: str | None
    workers: list[WorkerCandidate]
    agents: list[AgentCandidate]
    accounts: list[AccountCandidate]


async def _run(conn: AsyncConnection, run_id: int):
    r = tables.runs
    query = select(
        r.c.id, r.c.kind, r.c.failure_cause, r.c.repos, r.c.repo, r.c.dispatched_via, r.c.profile_id, r.c.worker_id
    ).where(r.c.id == run_id)
    return (await conn.execute(query)).one()


def _repos(run) -> list[str]:
    """The repos the run worked in: those it names, else its one repo."""
    named = [entry["repo"] for entry in run.repos or () if isinstance(entry, dict) and entry.get("repo")]
    return named or ([run.repo] if run.repo else [])


async def _accounts(conn: AsyncConnection, owner_id: int, project_id: int, run_id: int, names: dict[int, str]):
    """The owner's env accounts bound to the project: (secret id, ``AccountCandidate``) for each, and by secret id its
    state with the worker ids it is bound to (None: any), and the secret ids the run took."""
    s, a, b, ra = tables.secrets, tables.accounts, tables.secret_bindings, tables.run_accounts
    rows = await conn.execute(
        select(s.c.id, s.c.name, s.c.expires_at, a.c.paused_at, a.c.cooldown_until)
        .outerjoin_from(s, a, a.c.secret_id == s.c.id)
        .where(s.c.owner_id == owner_id, s.c.kind == "env", s.c.deleted_at.is_(None))
        .order_by(s.c.name)
    )
    secrets = rows.all()
    bound: dict[int, set[int | None]] = {}
    for row in await conn.execute(
        select(b.c.secret_id, b.c.worker_id).where(
            b.c.project_id == project_id, b.c.secret_id.in_([row.id for row in secrets])
        )
    ):
        bound.setdefault(row.secret_id, set()).add(row.worker_id)
    used = set((await conn.execute(select(ra.c.secret_id).where(ra.c.run_id == run_id))).scalars())
    now = datetime.now(UTC)
    found, ready = [], {}
    for row in secrets:
        if row.id not in bound:
            continue
        where = bound[row.id]
        state = account_state(row.paused_at, row.cooldown_until, row.expires_at, now)
        workers = ["any"] if None in where else sorted(names[w] for w in where if w in names)
        found.append(
            (row.id, AccountCandidate(name=row.name, state=state, workers=workers, used_by_run=row.id in used))
        )
        ready[row.id] = (state, where)
    return found, ready, used


def _ready_on(worker_id: int, ready: dict, found) -> list[tuple[int, str]]:
    """(secret id, name) of the accounts that are ready on worker ``worker_id``."""
    return [
        (secret_id, account.name)
        for secret_id, account in found
        if ready[secret_id][0] == "ready" and ready[secret_id][1] & {None, worker_id}
    ]


def _worker_view(row, profile, run, repos, project, accounts, cause):
    """(the candidate of worker ``row``, its sort key)."""
    found, ready, used = accounts
    reasons = worker_problems(row, profile, run.dispatched_via or "machine", run.kind)
    missing = [name for name in repos if f"{project}/{name}" not in (row.checkouts or {})]
    if missing:
        reasons.append(f"has no checkout of {', '.join(missing)}")
    mine = _ready_on(row.id, ready, found)
    if found and not mine:
        reasons.append("has no account of yours that is ready for this project")
    fit = not reasons
    held = len(repos) - len(missing)
    free = max(row.slots - row.holding, 0)
    runtimes = usable_runtimes(row, profile)
    models = sorted(
        {model for name in runtimes for model in ((row.runtimes or {}).get(name) or {}).get("models") or ()}
        if isinstance(row.runtimes, dict)
        else ()
    )
    old = row.id == run.worker_id
    other = any(secret_id not in used for secret_id, _ in mine)
    bias = 0
    if cause in OLD_FIRST:
        bias = 0 if old else 1
    elif cause in OLD_LAST:
        bias = 1 if old else 0
    elif cause in NEW_ACCOUNT_FIRST:
        bias = 0 if other else 1
    if fit:
        reasons = [
            "online with a free slot",
            f"has a checkout of {', '.join(repos) or 'the project'}",
            f"runs {', '.join(runtimes)}",
        ]
        if old:
            reasons.append("the worker the run failed on")
        if other and cause in NEW_ACCOUNT_FIRST:
            reasons.append("has an account the run did not use")
    candidate = WorkerCandidate(
        worker_id=row.id,
        worker=row.name,
        fit=fit,
        rank=None,
        reasons=reasons,
        is_run_worker=old,
        online=bool(row.online),
        free_slots=free,
        runtimes=runtimes,
        models=models,
        labels=list(row.labels or ()),
        accounts=[name for _, name in mine],
    )
    return candidate, (not fit, bias, -held, row.holding - row.slots, row.id)


def _agent_view(profile: Profile, rows, run, cause: str | None, writer: bool, project: str) -> AgentCandidate:
    reasons: list[str] = []
    if not writer:
        reasons.append("its owner is no writer of the project")
    if profile.deleted_at is not None:
        reasons.append("was deleted")
    via = run.dispatched_via or "machine"
    problems = [
        f"{row.name} {why}" for row in rows if (why := (worker_problems(row, profile, via, "chat") or [None])[0])
    ]
    if rows and len(problems) == len(rows):
        reasons.append(f"no worker of yours can take it: {'; '.join(problems)}")
    elif not rows:
        reasons.append(f"you have no worker that takes runs of project {project}")
    if cause == "missing_tool" and not profiles.allows(profile.permissions, "tool", "bash"):
        reasons.append("does not allow bash, which installing a missing program needs")
    fit = not reasons
    return AgentCandidate(
        profile_id=profile.id,
        handle=profile.handle,
        name=profile.name,
        is_default=profile.is_default,
        fit=fit,
        rank=None,
        reasons=reasons or ["a worker of yours can take it"],
    )


async def candidates_of(
    conn: AsyncConnection,
    *,
    project: str,
    project_id: int,
    role: str,
    owner_id: int,
    kind: str,
    state: str,
    run_id: int,
) -> RerunCandidates:
    """The candidates to run run ``run_id`` of project ``project`` again for its owner ``owner_id``, read on ``conn``,
    whatever state the run is in on it (the hub reads them in the transaction that fails the run); ``role`` is the
    owner's role in the project, which decides whether an agent is fit."""
    run = await _run(conn, run_id)
    repos = _repos(run)
    rows = (await conn.execute(workers_of(owner_id, project_id))).all()
    accounts = await _accounts(conn, owner_id, project_id, run_id, {r.id: r.name for r in rows})
    ap = tables.agent_profiles
    listed = (await conn.execute(profile_select().where(ap.c.project_id == project_id).order_by(ap.c.id))).all()
    by_id = {row.id: profile_of(row) for row in listed}
    profile = by_id.get(run.profile_id) or next((p for p in by_id.values() if p.is_default), None)
    if profile is None:  # a project that never made a profile: nothing limits the run
        profile = SimpleNamespace(handle=project, permissions={}, instructions=None)
    cause = run.failure_cause
    views = [_worker_view(row, profile, run, repos, project, accounts, cause) for row in rows]
    workers = []
    for place, (candidate, _) in enumerate(sorted((v for v in views if v[0].fit), key=lambda v: v[1]), 1):
        workers.append(candidate.model_copy(update={"rank": place}))
    workers += [v[0] for v in views if not v[0].fit]
    writer = has_role(role, "writer")
    agents = [_agent_view(p, rows, run, cause, writer, project) for p in by_id.values() if p.deleted_at is None]
    agents.sort(key=lambda a: (not a.fit, not a.is_default, a.handle))
    place = 0
    for index, agent in enumerate(agents):
        if agent.fit:
            place += 1
            agents[index] = agent.model_copy(update={"rank": place})
    return RerunCandidates(
        run_id=run_id,
        project=project,
        kind=kind,
        state=state,
        failure_cause=cause,
        workers=workers,
        agents=agents,
        accounts=[account for _, account in accounts[0]],
    )


async def rerun_candidates(engine, user: Principal, project: str, run_id: RunId) -> RerunCandidates:
    """The candidates to run the failed run ``run_id`` of ``project`` again, for its owner alone (``owned_run``);
    409 for a run that did not fail."""
    async with engine.begin() as conn:
        access, owned = await owned_run(conn, user, project, run_id, "list candidates for")
        if owned.state != "failed":
            raise HTTPException(409, f"run {run_id} is {owned.state}: only a failed run has candidates to run it again")
        return await candidates_of(
            conn,
            project=access.name,
            project_id=access.project_id,
            role=access.role,
            owner_id=owned.dispatched_by,
            kind=owned.kind,
            state=owned.state,
            run_id=run_id,
        )
