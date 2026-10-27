"""Admission of the Loop's work items (plan loop-engine, criterion a5): the last step of a tick, which checks each
queued work item before a run is handed out for it, oldest first.

The checks, in order; the first that fails is the work item's reason, and it stays queued, no run handed out:

1. ``breaker``: the Loop's breaker tripped. The Loop being on is the tick's own check: a tick of a Loop that is off
   does nothing, and turning it off cancelled every work item not finished.
2. ``concurrency``: the work items of its cycle running reach ``loop.business.max_running`` or
   ``loop.development.max_running`` (0 stops the cycle); for a ``deliver``, also the Loop's delivers holding a repo
   its plan's open steps name reach ``loop.repo_max_running``: one holds a repo while its plan run is in flight there,
   or while a merge request of its plan on that repo is not merged.
3. ``rate``: the runs the Loop handed out in the last 60 minutes (its ledger's ``dispatched`` rows) reach
   ``loop.dispatches_per_hour``.
4. ``worker``: for a ``build`` or a ``deliver``, worker ``loop.worker`` is named by no live worker serving the
   project, is offline or draining, or has no free slot (the runs it holds and those queued for it counted). With
   ``loop.pool`` or ``loop.labels`` set, they replace the pinned worker: no live, ready worker of the project, owned
   by a writer, holds the pool and every label, or none of them has a free slot.

A work item that passes them goes to the hand-out (``Dispatch``), which hands out its run in the tick's transaction or
says why not: a task run's agent type off, its run_as no writer, or its daily cap spent is ``type_refused``, as
``task_dispatch.hand_own_task`` refuses it (``loop.caps.hand_task``). A run handed out moves the work item to running
with the run, adds one to its attempts, writes the ledger row ``dispatched`` with the run, and counts against the
checks of the work items after it in the same pass. Without a hand-out for its kind a work item that passes stays
queued with no reason. A reason is written, with its ledger row (``waiting``, or ``ready`` once none holds), only when
it changes; its figures (the cap and the count, the worker and why) go into the work item's detail too, where the
routes read them, and are written again without a ledger row when they change under the same reason, so a tick that
finds the same reasons and figures writes nothing.

There is no budget among the checks (checkpoint of step 1, answer 1): the Loop reserves nothing, holds nothing back for
lack of money and settles no cost. What a run may cost is its own cap (``loop.caps``). Nothing here calls a model.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta

from sqlalchemy import Text, func, insert, literal, or_, select, update
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import merge_gate, runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server.loop.model import (
    ACT,
    BUILD,
    CYCLE_OF,
    DELIVER,
    DISPATCHED,
    QUEUED,
    READY,
    REASONS,
    RUNNING,
    WAITING,
    ItemView,
    View,
)
from evo_agents.hub.server.runs.models.placement import selector_of
from evo_agents.hub.server.runs.service.worker_fit import OFFLINE

log = logging.getLogger(__name__)

BREAKER, CONCURRENCY, RATE, WORKER, TYPE_REFUSED = REASONS[QUEUED]
WORKER_KINDS = (BUILD, DELIVER)  # the kinds whose runs go to worker loop.worker: author runs and plan runs
RATE_WINDOW = timedelta(hours=1)
NOT_MERGED = (*merge_gate.QUEUED_STATES, "paused")  # a merge request of the queue its repo still waits on
HANDED_REASONS = (WORKER, TYPE_REFUSED)  # why a hand-out hands out no run
# The figures of a reason admission writes into a work item's detail (``refusal``, ``_refused_by``); each hold puts
# its own in the place of those of the reason before.
FIGURE_KEYS = (
    *("cycle", "running", "max_running", "repos", "repo_max_running"),
    *("dispatched", "dispatches_per_hour", "worker", "why"),
)


@dataclass(frozen=True)
class LoopWorker:
    """Worker ``loop.worker`` as admission reads it: its id and owner (the Loop's author and plan runs go to it in its
    owner's name), its free slots, and ``down``, why it takes no run whatever its slots (none named, none found,
    offline, draining)."""

    name: str | None
    id: int | None = None
    owner_id: int | None = None
    free: int = 0
    down: str | None = None
    selector: tuple[str, ...] = ()  # set when loop.pool or loop.labels pick it: the runs carry it and pin no worker

    @property
    def problem(self) -> str | None:
        """Why it takes no run now; None when it takes one."""
        if self.down is not None:
            return self.down
        return None if self.free > 0 else f"worker {self.name} has no free slot"


@dataclass
class Capacity:
    """What the checks count, kept up to date as the pass hands out runs: whether the breaker tripped, the work items
    running by cycle, the delivers holding each repo, the runs handed out in the last hour, and worker loop.worker."""

    tripped: bool
    worker: LoopWorker
    running: Counter = field(default_factory=Counter)
    repos: Counter = field(default_factory=Counter)
    dispatched: int = 0

    def take(self, item: ItemView, repos: Iterable[str]) -> None:
        """Count the run just handed out for ``item``, a deliver holding ``repos``."""
        if item.kind == ACT:
            return  # a task of the plugin tasks holds no slot of the cycle and no hand-out of the hour
        self.running[CYCLE_OF[item.kind]] += 1
        self.dispatched += 1
        if item.kind == DELIVER:
            self.repos.update(set(repos))
        if item.kind in WORKER_KINDS:
            self.worker = replace(self.worker, free=self.worker.free - 1)


@dataclass(frozen=True)
class Handed:
    """What a hand-out did: the run it handed out, with the login the work item now stands for when that changes
    (``owner``), or why it handed none (``reason``, one of HANDED_REASONS, and ``why``). ``started`` is a work item of
    a plugin whose work is its own job (run kind job of a Loop rule): it runs with no run of the hub, and the plugin
    moves it."""

    run_id: int | None = None
    owner: str | None = None
    reason: str | None = None
    why: str | None = None
    started: bool = False


# A hand-out: (the tick's connection, the project, a work item that passed the checks, the tick's view, worker
# loop.worker) -> what it did, or None when it hands out no run for that kind of work item.
Dispatch = Callable[[AsyncConnection, int, ItemView, View, LoopWorker], Awaitable[Handed | None]]


def refusal(item: ItemView, repos: Sequence[str], capacity: Capacity, policy: Mapping) -> tuple[str, dict] | None:
    """(the reason, its figures) of the first check queued work item ``item`` fails, ``repos`` the repos of its plan
    for a deliver; None when it passes them all. ``policy`` is the loop section of the effective policy."""
    if capacity.tripped:
        return BREAKER, {}
    if item.kind == ACT:
        return None  # only the breaker holds an act: its task is no run
    cycle = CYCLE_OF[item.kind]
    most = int(policy[cycle]["max_running"])
    if capacity.running[cycle] >= most:
        return CONCURRENCY, {"cycle": cycle, "running": capacity.running[cycle], "max_running": most}
    if item.kind == DELIVER:
        per_repo = int(policy["repo_max_running"])
        full = sorted(repo for repo in set(repos) if capacity.repos[repo] >= per_repo)
        if full:
            return CONCURRENCY, {"repos": full, "repo_max_running": per_repo}
    rate = int(policy["dispatches_per_hour"])
    if capacity.dispatched >= rate:
        return RATE, {"dispatched": capacity.dispatched, "dispatches_per_hour": rate}
    if item.kind in WORKER_KINDS and capacity.worker.problem is not None:
        return WORKER, {"worker": capacity.worker.name, "why": capacity.worker.problem}
    return None


# What the checks read


def policy_selector(policy: Mapping) -> tuple[str, ...]:
    """The selector loop.pool and loop.labels make; empty when the policy sets neither (loop.worker then rules)."""
    return tuple(selector_of(policy.get("pool"), policy.get("labels")))


async def _selected(conn: AsyncConnection, project_id: int, selector: Sequence[str]) -> LoopWorker:
    """The workers a selector reaches (loop.pool, loop.labels, a plan's worker_labels): live, ready, not draining,
    serving the project, holding every label, and owned by a writer of the project (their runs go in the owner's
    name). The best of them, the most free slots, stands for the pick; its free slots are those of all of them."""
    from evo_agents.hub.server import worker_placement  # it reaches the runs package, which imports the Loop

    asked = ", ".join(selector)
    w, wp, g, r = tables.workers, tables.worker_projects, tables.grants, tables.runs
    held = (r.c.worker_id == w.c.id) & r.c.state.in_(runs.HELD_STATES)
    waiting = (r.c.pinned_worker_id == w.c.id) & (r.c.state == "queued")
    holding = select(func.count()).where(or_(held, waiting)).scalar_subquery()
    serves = select(wp.c.worker_id).where(wp.c.project_id == project_id)
    role = select(g.c.role).where(g.c.user_id == w.c.owner_id, g.c.project_id == project_id).scalar_subquery()
    query = (
        select(
            w.c.id,
            w.c.name,
            w.c.owner_id,
            w.c.slots,
            w.c.drained_at,
            (w.c.last_heartbeat_at >= func.now() - OFFLINE).label("online"),
            holding.label("holding"),
            role.label("role"),
            *worker_placement.columns(w),
        )
        .where(w.c.revoked_at.is_(None), w.c.id.in_(serves))
        .order_by(w.c.id)
    )
    found: list[tuple[int, object]] = []
    why = f"no live worker holding {asked} (the policy's loop.pool and loop.labels) serves the project"
    for row in await conn.execute(query):
        place = worker_placement.Placement.of(row)
        if not row.online or row.drained_at is not None or place.problems(selector):
            continue
        if not has_role(row.role, "writer"):
            why = f"the owner of every worker holding {asked} that serves the project is no writer on it"
            continue
        found.append((max(row.slots - row.holding, 0), row))
    if not found:
        return LoopWorker(asked, down=why, selector=tuple(selector))
    _, best = max(found, key=lambda pair: (pair[0], -pair[1].id))
    return LoopWorker(best.name, best.id, best.owner_id, sum(n for n, _ in found), selector=tuple(selector))


async def loop_worker(
    conn: AsyncConnection, project_id: int, name: str | None, selector: Sequence[str] = ()
) -> LoopWorker:
    """The worker of the Loop's author and plan runs. With a ``selector`` (loop.pool, loop.labels, a plan's
    worker_labels), the workers it reaches; with none, worker ``name`` (the policy's loop.worker): the live worker of
    that name, in any case, that serves the project, the oldest when several members name one so; its free slots are
    its slots less the runs it holds and those queued for it."""
    if selector:
        return await _selected(conn, project_id, selector)
    if not name:
        return LoopWorker(None, down="the policy names no loop.worker")
    w, wp, r = tables.workers, tables.worker_projects, tables.runs
    held = (r.c.worker_id == w.c.id) & r.c.state.in_(runs.HELD_STATES)
    waiting = (r.c.pinned_worker_id == w.c.id) & (r.c.state == "queued")
    holding = select(func.count()).where(or_(held, waiting)).scalar_subquery()
    serves = select(wp.c.worker_id).where(wp.c.project_id == project_id)
    query = (
        select(
            w.c.id,
            w.c.name,
            w.c.owner_id,
            w.c.slots,
            w.c.drained_at,
            (w.c.last_heartbeat_at >= func.now() - OFFLINE).label("online"),
            holding.label("holding"),
        )
        .where(func.lower(w.c.name) == name.lower(), w.c.revoked_at.is_(None), w.c.id.in_(serves))
        .order_by(w.c.id)
        .limit(1)
    )
    row = (await conn.execute(query)).first()
    if row is None:
        return LoopWorker(name, down=f"no live worker {name} (the policy's loop.worker) serves the project")
    down = None
    if not row.online:
        down = f"worker {row.name} is offline"
    elif row.drained_at is not None:
        down = f"worker {row.name} is draining"
    return LoopWorker(row.name, row.id, row.owner_id, max(row.slots - row.holding, 0), down)


def open_repos(body) -> tuple[str, ...]:
    """The repos the steps of plan ``body`` not done name, each once in plan order (``runs.plan_repo``)."""
    steps = body.get("steps") if isinstance(body, dict) and isinstance(body.get("steps"), list) else []
    names: list[str] = []
    for step in steps:
        if isinstance(step, dict) and step.get("status") == "done":
            continue
        name = (runs.plan_repo(body, step if isinstance(step, dict) else {}) or {}).get("repo")
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return tuple(names)


async def deliver_repos(conn: AsyncConnection, project_id: int, items: Sequence[ItemView]) -> dict[int, tuple]:
    """The repos of the plan of each deliver of ``items``, by work item."""
    plans = {item.id: item.plan_id for item in items if item.kind == DELIVER and item.plan_id}
    if not plans:
        return {}
    p = tables.plans
    wanted = sorted(set(plans.values()))
    query = select(p.c.plan_id, p.c.body).where(p.c.project_id == project_id, p.c.plan_id.in_(wanted))
    bodies = {plan_id: body for plan_id, body in await conn.execute(query)}
    return {item_id: open_repos(bodies.get(plan_id)) for item_id, plan_id in plans.items()}


async def _repos_held(conn: AsyncConnection, project_id: int, view: View) -> Counter:
    """Repo -> how many of the Loop's delivers hold it: a plan run of theirs in flight on it (the repos the run was
    dispatched with), or a merge request of their plan on it not merged."""
    i, r, q = tables.loop_items, tables.runs, tables.merge_queue
    holders: dict[str, set[str]] = defaultdict(set)
    flying = {
        item.run_id: item.plan_id or f"item {item.id}"
        for item in view.items
        if item.kind == DELIVER and item.state == RUNNING and item.run_id is not None
    }
    if flying:
        for run_id, repos in await conn.execute(select(r.c.id, r.c.repos).where(r.c.id.in_(sorted(flying)))):
            for entry in repos or []:
                if isinstance(entry, dict) and entry.get("repo"):
                    holders[entry["repo"]].add(flying[run_id])
    delivers = select(i.c.plan_id).where(i.c.project_id == project_id, i.c.kind == DELIVER, i.c.plan_id.is_not(None))
    open_merges = select(q.c.repo, q.c.plan_id).where(
        q.c.project_id == project_id, q.c.plan_id.in_(delivers.scalar_subquery()), q.c.state.in_(NOT_MERGED)
    )
    for repo, plan_id in await conn.execute(open_merges):
        holders[repo].add(plan_id)
    return Counter({repo: len(plans) for repo, plans in holders.items()})


async def _handed_lately(conn: AsyncConnection, project_id: int, view: View) -> int:
    i, e = tables.loop_items, tables.loop_item_events
    query = (
        select(func.count())
        .select_from(e.join(i, i.c.id == e.c.item_id))
        .where(i.c.project_id == project_id, i.c.kind != ACT, e.c.kind == DISPATCHED, e.c.at > view.now - RATE_WINDOW)
    )
    return int(await conn.scalar(query) or 0)


async def measure(conn: AsyncConnection, project_id: int, view: View, *, tripped: bool) -> Capacity:
    """What the checks count now, from ``view`` (every work item not finished) and the project's rows."""
    running = Counter(CYCLE_OF[item.kind] for item in view.items if item.state == RUNNING and item.kind != ACT)
    return Capacity(
        tripped=tripped,
        worker=await loop_worker(conn, project_id, view.policy.get("worker"), policy_selector(view.policy)),
        running=running,
        repos=await _repos_held(conn, project_id, view),
        dispatched=await _handed_lately(conn, project_id, view),
    )


# What the pass writes


async def _ledger(conn: AsyncConnection, item_id: int, kind: str, state: str, reason, detail, run_id=None) -> None:
    row = {"item_id": item_id, "kind": kind, "state": state, "reason": reason, "detail": dict(detail or {})}
    await conn.execute(insert(tables.loop_item_events).values(**row, run_id=run_id))


async def _running(conn: AsyncConnection, project_id: int, item: ItemView, handed: Handed) -> None:
    """Work item ``item`` running with the run ``handed`` names."""
    i = tables.loop_items
    values: dict = {"state": RUNNING, "reason": None, "run_id": handed.run_id, "updated_at": func.now()}
    values["attempts"] = i.c.attempts + 1
    if handed.owner:
        values["owner"] = handed.owner
    query = update(i).where(i.c.id == item.id, i.c.project_id == project_id, i.c.state == QUEUED)
    await conn.execute(query.values(values))
    await _ledger(conn, item.id, DISPATCHED, RUNNING, None, {}, run_id=handed.run_id)


async def _hold(conn: AsyncConnection, project_id: int, item: ItemView, refused: tuple[str, dict] | None) -> int:
    """Work item ``item`` stays queued with the reason of ``refused`` (None: none), and the figures of that reason in
    its detail in the place of those of the reason before (FIGURE_KEYS), which the routes show as ``waiting``
    (``items.figures``); 1 when its reason changed, with its ledger row. Figures that changed under the same reason are
    written with no ledger row; the same reason with the same figures writes nothing."""
    reason, figures = refused if refused is not None else (None, {})
    held = {key: item.detail.get(key) for key in FIGURE_KEYS if key in item.detail}
    if reason == item.reason and (reason is None or held == figures):
        return 0
    i = tables.loop_items
    kept = i.c.detail.op("-")(literal(list(FIGURE_KEYS), ARRAY(Text)))
    values: dict = {"detail": kept.op("||")(literal(dict(figures), JSONB))}
    if reason != item.reason:
        values |= {"reason": reason, "updated_at": func.now()}
    query = update(i).where(i.c.id == item.id, i.c.project_id == project_id, i.c.state == QUEUED)
    if await conn.scalar(query.values(values).returning(i.c.id)) is None or reason == item.reason:
        return 0
    await _ledger(conn, item.id, WAITING if reason else READY, QUEUED, reason, figures)
    return 1


def _refused_by(handed: Handed) -> tuple[str, dict]:
    if handed.reason not in HANDED_REASONS:
        raise ValueError(f"a hand-out that hands out no run says why with one of {HANDED_REASONS}: {handed!r}")
    return handed.reason, {"why": handed.why} if handed.why else {}


async def admit(
    conn: AsyncConnection, project_id: int, view: View, dispatch: Dispatch | None, *, tripped: bool
) -> Counter:
    """The admission pass of project ``project_id`` over the queued work items of ``view``, oldest first, handing
    out runs with ``dispatch`` (None: none yet), in the tick's transaction: how many runs it handed out
    (``dispatched``) and how many work items' reasons changed (``waiting``)."""
    report: Counter = Counter()
    queued = [item for item in view.items if item.state == QUEUED]
    if not queued:
        return report
    capacity = await measure(conn, project_id, view, tripped=tripped)
    repos = await deliver_repos(conn, project_id, queued)
    for item in queued:
        held = repos.get(item.id, ())
        refused = refusal(item, held, capacity, view.policy)
        handed = None
        if refused is None and dispatch is not None:
            handed = await dispatch(conn, project_id, item, view, capacity.worker)
        if handed is not None and (handed.run_id is not None or handed.started):
            await _running(conn, project_id, item, handed)
            capacity.take(item, held)
            report["dispatched"] += 1
            continue
        if handed is not None:
            refused = _refused_by(handed)
        report["waiting"] += await _hold(conn, project_id, item, refused)
    if report:
        log.info("loop admission", extra={"project_id": project_id, **report})
    return report


__all__ = [
    "BREAKER",
    "CONCURRENCY",
    "RATE",
    "TYPE_REFUSED",
    "WORKER",
    "Capacity",
    "Dispatch",
    "Handed",
    "LoopWorker",
    "admit",
    "deliver_repos",
    "loop_worker",
    "measure",
    "open_repos",
    "refusal",
]
