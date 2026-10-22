"""The claim of a worker (POST /v1/worker/claim): the claims waiting in this api process (``RunWakeups``), the run a
worker may take now, its lease, and the RunSpec it is handed, prompt, plan, budget, Curator role, skills and agent
profile included. The run's agent profile, at the version the run went under, decides which workers may take it and
the runtime of a run that asks for any (``service.profiles``). A run of a plugin's runtime goes only to a worker that
claims runs of it (``plugin_runtimes.claimable``), with the runtime's declaration as the plugin enabled on the run's
project has it now; one whose plugin is no longer enabled there fails at the claim, saying why. The run's
environment is the first of its profile's environment axis that the worker takes (``plugin_environments.picked``):
the host for an axis that allows all, else host or a plugin's environment the worker's owner accepted and its daemon
runs (``runs.ENVIRONMENTS``), recorded in runs.environment and handed with its declaration; a run whose allowlist
names no environment its project still registers fails at the claim, saying why. A run whose profile attaches agent
packs goes only to a worker whose daemon builds them and whose owner accepted each pack's plugin, with a presigned GET
of the bundle of each at the version the profile pins (``agent_packs.tickets``); one the hub cannot hand a pack fails
at the claim, saying why. A claim records the plugins of the run's project, each with its version and the digest of
the run's effective config (``plugin_configs.record_run_plugins``)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import BigInteger, Text, and_, any_, case, column, exists, func, literal, or_, select, true
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import author, curator, runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.blobs import MAX_GET_TTL
from evo_agents.hub.plans import PlanProblem, step_index
from evo_agents.hub.server import attachment_store
from evo_agents.hub.server.agent_packs import tickets
from evo_agents.hub.server.listen import Listener
from evo_agents.hub.server.loop.runs_of import item_of_run
from evo_agents.hub.server.plugin_configs import record_run_plugins
from evo_agents.hub.server.run_state import RUNS_CHANNEL, move_run
from evo_agents.hub.server.runs.models import (
    Claim,
    ClaimedBudget,
    CuratorSpec,
    PlanCopy,
    PluginRuntime,
    Run,
    RunEnvironment,
    RunPack,
    RunSkill,
    RunSpec,
    available,
)
from evo_agents.hub.server.runs.service import plugin_environments, plugin_runtimes
from evo_agents.hub.server.runs.service.profiles import (
    claim_clause,
    own_home,
    pick_runtime,
    run_origin,
    run_packs,
    run_profile,
    runtime_rule,
)
from evo_agents.hub.server.runs.service.views import current_plan, lost_here, run_view, steady, text_or_none
from evo_agents.hub.server.runs.service.worker_fit import writes
from evo_agents.hub.server.security import Principal
from evo_agents.plugins.run_environments import ContainerEnvironment
from evo_agents.plugins.runtimes import AcpRuntime

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
    drained_at, revoked_at, agent_version, dispatch_from, run_kinds, features, labels, plugins and shared. 403 when
    there is none."""
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
            w.c.features,
            w.c.labels,
            w.c.plugins,
            w.c.shared,
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
    offered: tuple[str, ...] = (),
    environments: tuple[str, ...] = (),
    plan_runs: bool,
    web_only: bool,
    review_runs: bool = False,
    judge_runs: bool = False,
    replay_runs: bool = False,
    history_runs: bool = False,
    author_runs: bool = False,
    chat_runs: bool = False,
    task_runs: bool = False,
    question_runs: bool = False,
    release_runs: bool = False,
    step_sessions: bool = False,
    clones: bool = False,
    steady: bool = True,
    shared: bool = False,
    profile_gate=None,
):
    """The oldest queued run the worker may take now, locked, passing over one another claim holds locked: of its owner,
    or, on a ``shared`` worker, of any member who holds writer on its project now (``_dispatcher_clause``), in
    ``projects``, pinned to no other worker, asking for any runtime (when it has one of the core) or one of
    ``runtimes`` or ``offered`` (runtimes of plugins), of a repo it has a checkout of (``pairs`` of project id and
    repo), and, while the worker is not ``steady``, not the next attempt of a run it lost (``lost_here``). A run of one
    step needs a checkout of its repo, and a plan run one of every repo in its repos and a daemon of runs.PLAN_RUN_AGENT
    or later (``plan_runs``); a review run one of every repo in its repos and a daemon that says it runs review runs
    (``review_runs``), and a judge run, a replay run and a history run likewise (``judge_runs``, ``replay_runs``,
    ``history_runs``; a history run has no repo); an author run one of its first repo, the project's harness, and a
    daemon that says it runs author runs (``author_runs``): the worker
    leaves out the other repos it has no checkout of. A chat run is pinned to the worker the hub picked for it and
    needs a daemon that says it runs chat runs (``chat_runs``). A task run needs a checkout of one of its repos when it
    has any, a daemon that says it runs task runs (``task_runs``) and, when its agent type names workers, to be one of
    them; a question run likewise, with a daemon that says it runs question runs (``question_runs``). A release run needs a checkout of its one repo and a daemon that says it runs release runs
    (``release_runs``). A plan run with a session per step needs a daemon that says it runs one (``step_sessions``).
    A worker whose daemon clones the repos it lacks (``clones``, runs.CLONES_CHECKOUTS) needs no checkout of any of
    them: those checkout rules hold only for an older one. A worker set to take runs dispatched from the web
    only (``web_only``) passes over the others, those dispatched before schema 0011 included; and it takes only the runs
    whose agent profile lets it (``profile_gate``, ``profiles.claim_clause``, with the plugins' ``environments`` it
    takes). Each row is the run's id, the runtime it asked for, its kind, its profile's runtime axis, its project's id,
    its profile's environment axis and the agent packs its profile attaches."""
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
    asked = [r.c.runtime.in_([*runtimes, *offered])]
    if runtimes:  # any takes a runtime of the core, never a plugin's
        asked.append(r.c.runtime == "any")
    rules = (runtime_rule(), r.c.project_id, runtime_rule("environment"), run_packs())
    query = select(r.c.id, r.c.runtime, r.c.kind, *rules).where(
        r.c.state == "queued",
        r.c.project_id.in_(projects),
        _dispatcher_clause(worker_id, owner_id, shared),
        or_(r.c.pinned_worker_id.is_(None), r.c.pinned_worker_id == worker_id),
        or_(*asked),
    )
    if not clones:  # an older daemon fails a run of a repo it has no checkout of
        query = query.where(
            case(
                (r.c.kind.in_(("plan", "review", "judge", "replay", "history", "release")), every_repo),
                (r.c.kind == "author", harness),
                (
                    r.c.kind == "chat",
                    true(),
                ),  # pinned to the worker the hub picked, which works without a repo it lacks
                (
                    r.c.kind.in_(("task", "question")),
                    or_(r.c.repos.is_(None), exists().select_from(needed).where(has_needed)),
                ),
                else_=own_repo,
            )
        )
    if web_only:
        query = query.where(r.c.dispatched_via == "web")
    if not plan_runs:
        query = query.where(r.c.kind != "plan")
    if not step_sessions:
        query = query.where(r.c.session_per_step.is_(False))
    said = {"review": review_runs, "judge": judge_runs, "replay": replay_runs, "history": history_runs}
    said |= {"author": author_runs, "chat": chat_runs, "release": release_runs}
    left_out = [kind for kind, runs_it in said.items() if not runs_it]  # the kinds its daemon does not say it runs
    if left_out:
        query = query.where(r.c.kind.not_in(left_out))
    typed = [kind for kind, runs_it in (("task", task_runs), ("question", question_runs)) if runs_it]
    query = query.where(or_(r.c.kind.not_in(("task", "question")), and_(r.c.kind.in_(typed), _task_worker(worker_id))))
    if not steady:
        query = query.where(~lost_here(worker_id))
    if profile_gate is not None:
        query = query.where(profile_gate)
    return query.order_by(r.c.id).limit(1).with_for_update(of=r, skip_locked=True)


def _dispatcher_clause(worker_id: int, owner_id: int, shared: bool):
    """Whose queued runs worker ``worker_id`` of ``owner_id`` may take: its owner's alone; on a shared worker, also
    those of another member who holds writer on the run's project now, none of the night shift's kinds
    (``runs.CURATOR_KINDS``), and on any of them only while every run the worker holds is of the same member, so the
    agents of two members never run side by side as the daemon's user (``docs/workers.md``, Shared workers)."""
    r = tables.runs
    if not shared:
        return r.c.dispatched_by == owner_id
    held = tables.runs.alias("held")
    member = and_(
        r.c.dispatched_by != owner_id, writes(r.c.dispatched_by, r.c.project_id), r.c.kind.not_in(runs.CURATOR_KINDS)
    )
    other = exists().where(
        held.c.worker_id == worker_id,
        held.c.state.in_(runs.HELD_STATES),
        held.c.dispatched_by != r.c.dispatched_by,
    )
    return and_(or_(r.c.dispatched_by == owner_id, member), ~other)


def _task_worker(worker_id: int):
    """Whether worker ``worker_id`` may take the task or question run: its agent type names no worker, or names it among the
    workers of the run's dispatcher (the type's run_as), never a shared worker of another member by the same name."""
    w, t, r = tables.workers, tables.project_agent_types, tables.runs
    own = (w.c.id == worker_id) & (w.c.owner_id == r.c.dispatched_by)
    name = select(func.lower(w.c.name)).where(own).correlate(r).scalar_subquery()
    return exists().where(
        t.c.id == r.c.agent_type_id, or_(func.cardinality(t.c.workers) == 0, name == any_(t.c.workers))
    )


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
        worker_id, owner_id, name, slots, reported, checkouts, drained_at, _, version, dispatch_from, *said = found
        kinds, features, labels, plugins, shared = said  # what its daemon says it runs: kinds of run, features
        if drained_at is not None:
            return None
        r = tables.runs
        holding = select(func.count()).select_from(r).where(r.c.worker_id == worker_id, r.c.state.in_(runs.HELD_STATES))
        if (await conn.execute(holding)).scalar_one() >= slots:
            return None
        runtimes = [runtime for runtime in runs.RUNTIMES if available((reported or {}).get(runtime))]
        offered = plugin_runtimes.claimable(reported, plugins, features)
        if not runtimes and not offered:
            return None
        served = {row.project_id: row.name for row in await conn.execute(_served(worker_id, owner_id))}
        pairs = _checkout_pairs(served, checkouts)
        clones = runs.CLONES_CHECKOUTS in (features or ())
        if not pairs and not clones:
            return None
        w = tables.workers
        steadiness = select(func.coalesce(steady(w), False)).where(w.c.id == worker_id)
        is_steady = (await conn.execute(steadiness)).scalar_one()
        environments = await plugin_environments.offered(conn, worker_id, plugins, features)
        query = _claimable(
            worker_id,
            owner_id,
            list(served),
            runtimes,
            pairs,
            offered=tuple(offered),
            environments=tuple(environments),
            plan_runs=runs.takes_plan_runs(version),
            web_only=dispatch_from == "web",
            review_runs="review" in (kinds or ()),
            judge_runs="judge" in (kinds or ()),
            replay_runs="replay" in (kinds or ()),
            history_runs="history" in (kinds or ()),
            author_runs="author" in (kinds or ()),
            chat_runs="chat" in (kinds or ()),
            task_runs="task" in (kinds or ()),
            question_runs="question" in (kinds or ()),
            release_runs="release" in (kinds or ()),
            step_sessions=runs.SESSION_PER_STEP in (features or ()),
            clones=clones,
            steady=is_steady,
            shared=shared,
            profile_gate=claim_clause(name, labels, runtimes, features, offered, environments, plugins),
        )
        row = (await conn.execute(query)).one_or_none()
        if row is None:
            return None
        run_id, asked, kind, rule, project_id, environment_rule, packs = row
        project = served[project_id]
        placed = await _placed_or_failed(conn, run_id, asked if asked in offered else None, project_id, project)
        if placed is not None:
            placed = await _settled_or_failed(
                conn, run_id, placed, (environment_rule, environments), project_id, (packs, blobs)
            )
        if placed is None:
            return None
        skill = None
        if kind == "author":
            skill = await author_skill(conn)
            refused = _skill_refusal(skill, blobs)
            if refused is not None:
                await move_run(conn, run_id, "queued", "failed", "reaper", reason=refused, error=refused)
                log.warning("author run failed at its claim", extra={"run_id": run_id, "worker_id": worker_id})
                return None
        runtime = pick_runtime(asked, runtimes, rule)
        await move_run(
            conn,
            run_id,
            "queued",
            "leased",
            "worker",
            reason=f"worker {name} claimed it",
            columns={"worker_id": worker_id, "runtime": runtime, "environment": placed.environment},
            token_id=user.token_id,
            lease=lease,
        )
        await record_run_plugins(conn, run_id)  # the version and config digest of each plugin of its project
        spec = await _run_spec(conn, run_id, tuple(features or ()))
        if skill is not None:
            spec.skills = [await _skill_ticket(blobs, skill)]
        if placed.runtime is not None:
            spec.plugin_runtime = PluginRuntime(**placed.runtime.to_json())
        if placed.container is not None:
            spec.environment = RunEnvironment(**placed.container.to_json())
        spec.packs = list(placed.packs)
        if gone is not None and await gone():
            raise ClaimAbandoned(run_id)  # leaving the block rolls the transaction back
    log.info("run claimed", extra={"run_id": run_id, "worker_id": worker_id, "runtime": runtime})
    return spec


def _checkout_pairs(served: dict[int, str], checkouts) -> list[tuple[int, str]]:
    """(project id, repo) of each of the worker's ``checkouts`` (keyed <project>/<repo>) in a project it ``served``."""
    by_name = {project: project_id for project_id, project in served.items()}
    pairs = []
    for checkout in checkouts or {}:
        project, _, repo = checkout.partition("/")
        if project in by_name and repo:
            pairs.append((by_name[project], repo))
    return pairs


@dataclass(frozen=True)
class Placed:
    """Where a claimed run's agent runs, beyond the core: the declaration of the plugin's runtime it asked for, and
    the environment of a plugin its claim picked with its declaration (None for both: the host)."""

    project: str
    runtime: AcpRuntime | None = None
    environment: str | None = None
    container: ContainerEnvironment | None = None
    packs: tuple[RunPack, ...] = ()


async def _placed_or_failed(
    conn: AsyncConnection, run_id: int, runtime: str | None, project_id: int, project: str
) -> Placed | None:
    """Placed with the declaration of the plugin's runtime ``runtime`` (None for a runtime of the core) that run
    ``run_id`` asks for; None once the run failed for want of one (``_declared_or_failed``)."""
    if runtime is None:
        return Placed(project)
    declaration = await _declared_or_failed(conn, run_id, runtime, project_id, project)
    return None if declaration is None else Placed(project, declaration)


async def _environment_or_failed(
    conn: AsyncConnection, run_id: int, placed: Placed, rule: dict | None, environments: list[str], project_id: int
) -> Placed | None:
    """``placed`` with the environment run ``run_id`` runs in, picked from its profile's environment axis ``rule``
    among host and the ``environments`` of plugins the worker takes, with the declaration its project has now; None
    once the run failed, as the reaper, for want of one: the plugin was disabled there since."""
    place = await plugin_environments.picked(conn, project_id, rule, environments)
    if place is not None:
        environment, container = place
        return replace(placed, environment=None if container is None else environment, container=container)
    allow = ", ".join(plugin_environments.allowlist({"environment": rule}) or [])
    refused = (
        f"no environment the run's agent profile allows ({allow}) is registered in project {placed.project} now: the "
        "plugin was disabled there, turned off or removed since the run was dispatched, or its version declares the "
        "environment no more; enable it and dispatch the run again"
    )
    await move_run(conn, run_id, "queued", "failed", "reaper", reason=refused, error=refused)
    log.warning("run of a plugin's environment failed at its claim", extra={"run_id": run_id})
    return None


async def _settled_or_failed(
    conn: AsyncConnection, run_id: int, placed: Placed, where: tuple, project_id: int, packs: tuple
) -> Placed | None:
    """``placed`` with its environment, from ``where`` (the profile's environment axis, and the environments of plugins
    the worker takes; ``_environment_or_failed``), and with the agent packs the profile attaches, from ``packs`` (them,
    and the blob store; ``_packs_or_failed``); None once the run failed for want of either."""
    placed = await _environment_or_failed(conn, run_id, placed, *where, project_id)
    if placed is None or not packs[0]:
        return placed
    return await _packs_or_failed(conn, run_id, placed, project_id, *packs)


async def _packs_or_failed(
    conn: AsyncConnection, run_id: int, placed: Placed, project_id: int, packs: list[dict], blobs
) -> Placed | None:
    """``placed`` with the agent packs ``packs`` of run ``run_id``'s profile, each with a presigned GET of its plugin's
    bundle from the blob store ``blobs``; None once the run failed, as the reaper, for want of one."""
    found, refused = await tickets(conn, blobs, project_id, placed.project, packs)
    if refused is None:
        return replace(placed, packs=tuple(found))
    await move_run(conn, run_id, "queued", "failed", "reaper", reason=refused, error=refused)
    log.warning("run of agent packs failed at its claim", extra={"run_id": run_id})
    return None


async def _declared_or_failed(conn: AsyncConnection, run_id: int, runtime: str, project_id: int, project: str):
    """The declaration of the plugin's runtime ``runtime`` that run ``run_id`` asks for, as project ``project`` has it
    now; None once the run failed, as the reaper, for want of one: the plugin was disabled there since."""
    declaration = await plugin_runtimes.declared(conn, project_id, runtime)
    if declaration is not None:
        return declaration
    plugin = runtime.partition("/")[0]
    refused = (
        f"runtime {runtime} is no longer registered in project {project}: plugin {plugin} was disabled there, turned "
        "off or removed since the run was dispatched, or its version declares the runtime no more; enable it and "
        "dispatch the run again"
    )
    await move_run(conn, run_id, "queued", "failed", "reaper", reason=refused, error=refused)
    log.warning("run of a plugin's runtime failed at its claim", extra={"run_id": run_id, "runtime": runtime})
    return None


OWN_PROMPTS = (
    "review",
    "chat",
    "task",
    "judge",
    "replay",
    "history",
    "release",
    "question",
)  # whose prompt their module builds


async def _own_prompt(conn: AsyncConnection, view: Run, features: tuple[str, ...] = ()) -> str:
    """The prompt of run ``view`` of one of OWN_PROMPTS, as the module of its kind builds it for a worker whose daemon
    says it does ``features``; each module queues runs through this package, so it is imported here."""
    if view.kind == "review":
        from evo_agents.hub.server.collect import review_prompt

        return await review_prompt(conn, view)
    if view.kind == "chat":
        from evo_agents.hub.server.chat_runs import chat_prompt

        repos = [repo.model_dump() for repo in view.repos or []]
        return await chat_prompt(conn, view.id, view.branch, repos, features)
    if view.kind == "task":
        from evo_agents.hub.server.task_dispatch import task_prompt

        return await task_prompt(conn, view)
    if view.kind == "question":
        from evo_agents.hub.server.question_dispatch import question_prompt

        return await question_prompt(conn, view)
    if view.kind == "judge":  # the merge queue's on no plan, else the Curator's
        from evo_agents.hub.server.changes import judge_prompt
        from evo_agents.hub.server.merge_judge import merge_prompt

        return await merge_prompt(conn, view) or await judge_prompt(conn, view)
    if view.kind == "history":
        from evo_agents.hub.history import RUN_PROMPT

        return RUN_PROMPT
    if view.kind == "release":
        from evo_agents.hub.server.release_claims import release_prompt  # it reads runs through this package

        return await release_prompt(conn, view)
    from evo_agents.hub.server.replays import run_prompt

    return await run_prompt(conn, view)


async def _run_spec(conn: AsyncConnection, run_id: int, features: tuple[str, ...] = ()) -> RunSpec:
    """The claim of run ``run_id`` by a worker whose daemon says it does ``features`` (``runs.WORKER_FEATURES``): an
    author run's prompt lets its agent ask decisions only on a daemon that does ``runs.AUTHOR_DECISIONS``, and a chat
    run's tells its agent to open the repos it needs only on one that does ``runs.LAZY_CHAT_REPOS``. The prompt of a run
    under an agent profile that is not its project's default ends with the index of its memories
    (``memory_upkeep.with_index``)."""
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
    if view.kind in OWN_PROMPTS:
        prompt = await _own_prompt(conn, view, features)
        title = view.title
    elif view.kind == "author":
        skill = await author_skill(conn)
        repos = [repo.model_dump() for repo in view.repos or []]
        version = skill.version if skill is not None else 0
        target = (view.plan_id, view.plan_revision) if view.plan_id else None
        prompt = author.build_author_prompt(
            view.project,
            view.dispatched_by,
            view.request or "",
            repos,
            version,
            plan=target,
            decisions=runs.AUTHOR_DECISIONS in features,
            loop=await item_of_run(conn, view.id) is not None,
        )
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
    from evo_agents.hub.server.memory_upkeep import with_index  # it reads memories through their routes' module

    prompt = await with_index(conn, run_id, prompt)  # the memories of an agent profile, not the project's default
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
        session_per_step=view.session_per_step,
        profile=await run_profile(conn, run_id),
        own_home=await own_home(conn, run_id),
        origin=await run_origin(conn, run_id),
        release=await _release_spec(conn, view),
        attachments=await _opening_files(conn, view),
        chat_message_id=view.chat_message_id if view.kind == "chat" else None,
    )


async def _opening_files(conn: AsyncConnection, view: Run) -> list:
    """The files of the chat message that opened chat run ``view``; none for any other run."""
    if view.kind != "chat" or view.chat_message_id is None:
        return []
    return (await attachment_store.of_messages(conn, [view.chat_message_id])).get(view.chat_message_id, [])


async def _release_spec(conn: AsyncConnection, view: Run):
    """A release run's cycle, mode and start commit (``release_claims.release_claim``); None for any other run."""
    if view.kind != "release":
        return None
    from evo_agents.hub.server.release_claims import release_claim  # it reads runs through this package

    return await release_claim(conn, view.id)


async def _curator_spec(conn: AsyncConnection, project_id: int, view: Run) -> CuratorSpec | None:
    from evo_agents.hub.server.changes import run_curator  # it reads runs through this package
    from evo_agents.hub.server.history import history_claim  # it queues runs through this package

    found = await run_curator(conn, project_id, view)
    if found is None and view.kind == "judge":  # a judge run of the merge queue is guarded as the Curator's are
        from evo_agents.hub.server.merge_judge import merge_claim

        found = await merge_claim(conn, view)
    if found is not None and view.kind == "history":
        found.update(await history_claim(conn, view.id))
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
