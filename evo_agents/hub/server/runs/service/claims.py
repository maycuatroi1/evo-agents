"""The claim of a worker (POST /v1/worker/claim): the claims waiting in this api process (``RunWakeups``), the run a
worker may take now, its lease, and the RunSpec it is handed, prompt, plan, budget, Curator role and skills
included."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import BigInteger, Text, case, column, exists, func, literal, or_, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import author, curator, runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.blobs import MAX_GET_TTL
from evo_agents.hub.plans import PlanProblem, step_index
from evo_agents.hub.server.listen import Listener
from evo_agents.hub.server.run_state import RUNS_CHANNEL, move_run
from evo_agents.hub.server.runs.models import (
    Claim,
    ClaimedBudget,
    CuratorSpec,
    PlanCopy,
    Run,
    RunSkill,
    RunSpec,
    available,
)
from evo_agents.hub.server.runs.service.views import current_plan, lost_here, run_view, steady, text_or_none
from evo_agents.hub.server.security import Principal

log = logging.getLogger("evo_agents.hub.server.runs")  # the package's one logger, as before it was split

CLAIM_POLL_SECONDS = 5.0  # a waiting claim looks again this often, notification or not
WRITER_ROLES = [role for role in ("reader", "writer", "admin") if has_role(role, "writer")]


class RunWakeups:
    """The claims waiting in this api process, woken by RUNS_CHANNEL on the process's one LISTEN (``listen``),
    opened by the first claim or stream. A notification, or a newer claim of the same worker, wakes every waiting
    claim, which then looks at the queue again; a claim also looks every CLAIM_POLL_SECONDS, so it never depends on
    the connection being up."""

    def __init__(self, listener: Listener):
        self._listener = listener
        listener.on(RUNS_CHANNEL, lambda payload: self.wake())
        self._event = asyncio.Event()  # set by the next wake-up, then replaced
        self.generation = 0  # counts wake-ups; a claim waits only while it has not changed
        self._tickets: dict[int, int] = {}  # worker id: the number of its newest claim

    def start(self) -> None:
        self._listener.start()

    def wake(self) -> None:
        self.generation += 1
        self._event.set()
        self._event = asyncio.Event()

    async def wait(self, seen: int, timeout: float) -> None:
        """Return once something woke the claims after generation ``seen``, or after ``timeout`` seconds."""
        if self.generation != seen:
            return
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except TimeoutError:
            pass

    async def ticket(self, worker_id: int) -> int:
        """The number of a new claim of ``worker_id``, which makes any older one of it end without a run."""
        number = self._tickets.get(worker_id, 0) + 1
        self._tickets[worker_id] = number
        self.wake()
        return number

    def current(self, worker_id: int, number: int) -> bool:
        return self._tickets.get(worker_id) == number

    def done(self, worker_id: int, number: int) -> None:
        if self._tickets.get(worker_id) == number:
            del self._tickets[worker_id]


async def worker_of(conn: AsyncConnection, user: Principal):
    """The row of the worker whose token made the request, locked: id, owner_id, name, slots, runtimes, checkouts,
    drained_at, revoked_at, agent_version, dispatch_from and run_kinds. 403 when there is none."""
    w = tables.workers
    query = (
        select(
            w.c.id,
            w.c.owner_id,
            w.c.name,
            w.c.slots,
            w.c.runtimes,
            w.c.checkouts,
            w.c.drained_at,
            w.c.revoked_at,
            w.c.agent_version,
            w.c.dispatch_from,
            w.c.run_kinds,
        )
        .where(w.c.token_id == user.token_id)
        .with_for_update()
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None or row.revoked_at is not None:
        raise HTTPException(403, "this worker token belongs to no live worker: join the machine again")
    return row


def _served(worker_id: int, owner_id: int):
    """The projects worker ``worker_id`` serves on which its owner holds writer: their ids and names."""
    wp, p, g = tables.worker_projects, tables.projects, tables.grants
    writer = exists().where(g.c.user_id == owner_id, g.c.project_id == wp.c.project_id, g.c.role.in_(WRITER_ROLES))
    return (
        select(wp.c.project_id, p.c.name)
        .join_from(wp, p, p.c.id == wp.c.project_id)
        .where(wp.c.worker_id == worker_id, writer)
    )


def _claimable(
    worker_id: int,
    owner_id: int,
    projects: list[int],
    runtimes: list[str],
    pairs: list[tuple[int, str]],
    *,
    plan_runs: bool,
    web_only: bool,
    review_runs: bool = False,
    judge_runs: bool = False,
    author_runs: bool = False,
    steady: bool = True,
):
    """The oldest queued run the worker may take now, locked, passing over one another claim holds locked: of its
    owner, in ``projects``, pinned to no other worker, asking for any runtime or one of ``runtimes``, of a repo it
    has a checkout of (``pairs`` of project id and repo), and, while the worker is not ``steady``, not the next
    attempt of a run it lost (``lost_here``). A run of one step needs a checkout of its repo, and a plan
    run one of every repo in its repos and a daemon of runs.PLAN_RUN_AGENT or later (``plan_runs``); a review run one
    of every repo in its repos and a daemon that says it runs review runs (``review_runs``), and a judge run likewise
    (``judge_runs``); an author run one of its first repo, the project's harness, and a daemon that says it runs
    author runs (``author_runs``): the worker leaves out the other repos it has no checkout of. A worker set to take
    runs dispatched from the web only (``web_only``) passes over the others, those dispatched before schema 0011
    included."""
    r = tables.runs
    pids = literal([pid for pid, _ in pairs], ARRAY(BigInteger))
    repos = literal([repo for _, repo in pairs], ARRAY(Text))
    held = func.unnest(pids, repos).table_valued("project_id", "repo").render_derived(name="c")
    checkouts = select(held.c.project_id, held.c.repo).cte("checkouts")
    needed = func.jsonb_array_elements(r.c.repos).table_valued(column("entry", JSONB)).render_derived(name="needed")
    # two levels down, the run is correlated by name: auto-correlation reaches only the enclosing level
    has_needed = (
        exists()
        .where(checkouts.c.project_id == r.c.project_id, checkouts.c.repo == needed.c.entry["repo"].astext)
        .correlate(r, needed)
    )
    every_repo = ~exists().select_from(needed).where(~has_needed)
    own_repo = exists().where(checkouts.c.project_id == r.c.project_id, checkouts.c.repo == r.c.repo)
    harness = exists().where(checkouts.c.project_id == r.c.project_id, checkouts.c.repo == r.c.repos[0]["repo"].astext)
    query = select(r.c.id, r.c.runtime, r.c.kind).where(
        r.c.state == "queued",
        r.c.project_id.in_(projects),
        r.c.dispatched_by == owner_id,
        or_(r.c.pinned_worker_id.is_(None), r.c.pinned_worker_id == worker_id),
        or_(r.c.runtime == "any", r.c.runtime.in_(runtimes)),
        case((r.c.kind.in_(("plan", "review", "judge")), every_repo), (r.c.kind == "author", harness), else_=own_repo),
    )
    if web_only:
        query = query.where(r.c.dispatched_via == "web")
    if not plan_runs:
        query = query.where(r.c.kind != "plan")
    if not review_runs:
        query = query.where(r.c.kind != "review")
    if not judge_runs:
        query = query.where(r.c.kind != "judge")
    if not author_runs:
        query = query.where(r.c.kind != "author")
    if not steady:
        query = query.where(~lost_here(worker_id))
    return query.order_by(r.c.id).limit(1).with_for_update(of=r, skip_locked=True)


class ClaimAbandoned(Exception):
    """The worker hung up before its claim was answered; the lease the claim took is rolled back."""

    def __init__(self, run_id: int):
        super().__init__(f"the worker hung up before run {run_id} was handed to it")
        self.run_id = run_id


@dataclass(frozen=True)
class HeldSkill:
    """The latest version of a global skill, as the hub holds it."""

    name: str
    version: int
    sha256: str
    size: int


def _global_skill(name: str):
    """The latest version of the global skill ``name`` (ignoring case, as names are unique): name, version, sha256,
    size."""
    sk, v = tables.skills, tables.skill_versions
    return (
        select(sk.c.name, v.c.version, v.c.sha256, v.c.size)
        .join_from(sk, v, v.c.skill_id == sk.c.id)
        .where(sk.c.scope == "global", func.lower(sk.c.name) == name.lower())
        .order_by(v.c.version.desc())
        .limit(1)
    )


async def author_skill(conn: AsyncConnection) -> HeldSkill | None:
    """The version of ``author.AUTHOR_SKILL`` an author run claimed now gets; None when the hub has none."""
    row = (await conn.execute(_global_skill(author.AUTHOR_SKILL))).one_or_none()
    return None if row is None else HeldSkill(*row)


def _skill_refusal(skill: HeldSkill | None, blobs) -> str | None:
    """Why an author run cannot be handed its skill now, which fails it at its claim; None when it can."""
    name = author.AUTHOR_SKILL
    if skill is None:
        return (
            f"the hub holds no global skill {name}, which the agent of an author run writes the plan with: a hub admin "
            f"publishes it with `evo-agents hub skills publish <agent-skills>/skills/{name}` (scope global), then "
            "dispatch the author run again"
        )
    if blobs is None:
        return (
            f"the hub has no blob store, so it cannot hand the worker the bundle of skill {name} (version "
            f"{skill.version}): configure the blob store of the hub, then dispatch the author run again"
        )
    return None


async def _skill_ticket(blobs, skill: HeldSkill) -> RunSkill:
    """A presigned GET of ``skill``'s bundle, for the worker that claimed the run; it works MAX_GET_TTL."""
    expires_at = datetime.now(UTC) + MAX_GET_TTL  # taken before signing, so never later than the URL
    filename = f"{skill.name}-v{skill.version}.tar.gz"
    url = await asyncio.to_thread(blobs.presign_get, skill.sha256, MAX_GET_TTL, filename=filename)
    return RunSkill(
        name=skill.name, version=skill.version, sha256=skill.sha256, size=skill.size, url=url, expires_at=expires_at
    )


async def _try_claim(
    engine,
    user: Principal,
    lease: timedelta,
    gone: Callable[[], Awaitable[bool]] | None = None,
    blobs=None,
) -> RunSpec | None:
    """Lease the run the worker of ``user`` may take now, if any, for ``lease`` (see the docstring of the package).
    When ``gone`` says the worker hung up once the run is leased, raise ClaimAbandoned before the transaction
    commits, which rolls the lease back. An author run comes with a presigned GET of its skill from the blob store
    ``blobs``; one the hub cannot hand its skill (none published, or no blob store) fails here, as the reaper, saying
    why, and the claim takes nothing this time."""
    async with engine.begin() as conn:
        found = await worker_of(conn, user)
        worker_id, owner_id, name, slots, reported, checkouts, drained_at, _, version, dispatch_from, kinds = found
        if drained_at is not None:
            return None
        r = tables.runs
        holding = select(func.count()).select_from(r).where(r.c.worker_id == worker_id, r.c.state.in_(runs.HELD_STATES))
        if (await conn.execute(holding)).scalar_one() >= slots:
            return None
        runtimes = [runtime for runtime in runs.RUNTIMES if available((reported or {}).get(runtime))]
        if not runtimes:
            return None
        served = {row.project_id: row.name for row in await conn.execute(_served(worker_id, owner_id))}
        by_name = {project: project_id for project_id, project in served.items()}
        pairs = []
        for checkout in checkouts or {}:
            project, _, repo = checkout.partition("/")
            if project in by_name and repo:
                pairs.append((by_name[project], repo))
        if not pairs:
            return None
        w = tables.workers
        steadiness = select(func.coalesce(steady(w), False)).where(w.c.id == worker_id)
        is_steady = (await conn.execute(steadiness)).scalar_one()
        query = _claimable(
            worker_id,
            owner_id,
            list(served),
            runtimes,
            pairs,
            plan_runs=runs.takes_plan_runs(version),
            web_only=dispatch_from == "web",
            review_runs="review" in (kinds or ()),
            judge_runs="judge" in (kinds or ()),
            author_runs="author" in (kinds or ()),
            steady=is_steady,
        )
        row = (await conn.execute(query)).one_or_none()
        if row is None:
            return None
        run_id, asked, kind = row
        skill = None
        if kind == "author":
            skill = await author_skill(conn)
            refused = _skill_refusal(skill, blobs)
            if refused is not None:
                await move_run(conn, run_id, "queued", "failed", "reaper", reason=refused, error=refused)
                log.warning("author run failed at its claim", extra={"run_id": run_id, "worker_id": worker_id})
                return None
        runtime = runtimes[0] if asked == "any" else asked
        await move_run(
            conn,
            run_id,
            "queued",
            "leased",
            "worker",
            reason=f"worker {name} claimed it",
            columns={"worker_id": worker_id, "runtime": runtime},
            token_id=user.token_id,
            lease=lease,
        )
        spec = await _run_spec(conn, run_id)
        if skill is not None:
            spec.skills = [await _skill_ticket(blobs, skill)]
        if gone is not None and await gone():
            raise ClaimAbandoned(run_id)  # leaving the block rolls the transaction back
    log.info("run claimed", extra={"run_id": run_id, "worker_id": worker_id, "runtime": runtime})
    return spec


async def _run_spec(conn: AsyncConnection, run_id: int) -> RunSpec:
    view = await run_view(conn, run_id)
    r, revisions = tables.runs, tables.plan_revisions
    project_id = (await conn.execute(select(r.c.project_id).where(r.c.id == run_id))).scalar_one()
    dispatched = select(revisions.c.body).where(
        revisions.c.project_id == project_id,
        revisions.c.plan_id == view.plan_id,
        revisions.c.revision == view.plan_revision,
    )
    body = (await conn.execute(dispatched)).scalar_one_or_none()
    plan = body if body is not None else {"id": view.plan_id, "steps": []}
    copy, verify = None, None
    if view.kind == "review":
        from evo_agents.hub.server.collect import review_prompt  # it queues runs through this package

        prompt = await review_prompt(conn, view)
        title = view.title
    elif view.kind == "author":
        skill = await author_skill(conn)
        repos = [repo.model_dump() for repo in view.repos or []]
        version = skill.version if skill is not None else 0
        target = (view.plan_id, view.plan_revision) if view.plan_id else None
        prompt = author.build_author_prompt(
            view.project, view.dispatched_by, view.request or "", repos, version, plan=target
        )
        title = view.title
    elif view.kind == "judge":
        from evo_agents.hub.server.changes import judge_prompt  # it queues runs through this package

        prompt = await judge_prompt(conn, view)
        title = view.title
    elif view.kind == "plan":
        current = await current_plan(conn, project_id, view.plan_id)
        body, revision = current if current else (plan, view.plan_revision)  # the plan gone: as dispatched
        copy = PlanCopy(body=body, revision=revision)
        prompt = runs.build_plan_prompt(copy.body, [repo.model_dump() for repo in view.repos or []])
        title = view.title
    else:
        try:
            step = plan["steps"][step_index(plan, view.step_key)]
        except (PlanProblem, KeyError, TypeError):
            step = None
        if not isinstance(step, dict):
            step = {"id": view.step_key}
        prompt = runs.build_prompt(plan, step, {"repo": view.repo, "branch": view.branch})
        title = text_or_none(step.get("title"))
        verify = runs.verify_of(step)
    return RunSpec(
        id=view.id,
        kind=view.kind,
        project=view.project,
        plan_id=view.plan_id,
        step_key=view.step_key,
        title=title,
        plan_revision=view.plan_revision,
        attempt=view.attempt,
        max_attempts=view.max_attempts,
        parent_run_id=view.parent_run_id,
        resume_of_run_id=view.resume_of_run_id,
        session_id=view.session_id,
        runtime=view.runtime,
        model=view.model,
        mode=view.mode,
        approval=view.approval,
        timeout_min=view.timeout_min,
        repo=view.repo,
        branch=view.branch,
        repos=view.repos,
        lease_expires_at=view.lease_expires_at,
        prompt=prompt,
        plan=copy,
        budget=await _claimed_budget(conn, view),
        curator=await _curator_spec(conn, project_id, view),
        verify=verify,
    )


async def _curator_spec(conn: AsyncConnection, project_id: int, view: Run) -> CuratorSpec | None:
    from evo_agents.hub.server.changes import run_curator  # it reads runs through this package

    found = await run_curator(conn, project_id, view)
    return None if found is None else CuratorSpec(**found)


async def _claimed_budget(conn: AsyncConnection, view: Run) -> ClaimedBudget | None:
    """The budget of run ``view`` with what it spent already: the agent time it counted (a run that resumes a parked
    one starts with that run's), and the cost of the run it resumes, whose session it goes on in."""
    if view.budget is None:
        return None
    spent_usd = 0.0
    if view.resume_of_run_id is not None:
        r = tables.runs
        usage = (await conn.execute(select(r.c.usage).where(r.c.id == view.resume_of_run_id))).scalar_one_or_none()
        spent_usd = curator.run_cost(usage)
    return ClaimedBudget(**view.budget.model_dump(), spent_usd=spent_usd, spent_seconds=view.run_seconds)


async def wait_for_run(
    engine,
    wakeups: RunWakeups,
    user: Principal,
    lease: timedelta,
    wait: float,
    is_disconnected: Callable[[], Awaitable[bool]],
    blobs,
) -> Claim:
    """Wait up to ``wait`` seconds for a run the worker of ``user`` may take, and lease it for ``lease``: a claim
    whose worker hung up (``is_disconnected``) takes nothing."""
    wakeups.start()
    async with engine.begin() as conn:
        worker_id = (await worker_of(conn, user))[0]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    number = await wakeups.ticket(worker_id)
    try:
        while True:
            # Hung up already: a run leased now would wait out its lease with nobody to run it.
            if await is_disconnected():
                return Claim(run=None)
            seen = wakeups.generation
            try:
                spec = await _try_claim(engine, user, lease, is_disconnected, blobs)
            except ClaimAbandoned as exc:
                log.info("claim abandoned; the run stays queued", extra={"run_id": exc.run_id, "worker_id": worker_id})
                wakeups.wake()  # the run is queued again: the other claims waiting here look at it
                return Claim(run=None)
            if spec is not None:
                return Claim(run=spec)
            remaining = deadline - loop.time()
            if remaining <= 0 or not wakeups.current(worker_id, number):
                return Claim(run=None)
            await wakeups.wait(seen, min(remaining, CLAIM_POLL_SECONDS))
    finally:
        wakeups.done(worker_id, number)
