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
the run's effective config (``plugin_configs.record_run_plugins``). The next attempt of a plan run the hub lost, claimed
by the worker that lost it, names that attempt and its agent's session (``lost_attempt``), for the worker to go on in
when it still has them; claimed by any other worker, it starts afresh. What the claim asks of a run is
``claim_rules``; the RunSpec, and where the run's agent runs, ``run_specs``."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server import agent_placement, run_autonomy, worker_placement
from evo_agents.hub.server.listen import Listener
from evo_agents.hub.server.plugin_configs import record_run_plugins
from evo_agents.hub.server.run_state import RUNS_CHANNEL, move_run
from evo_agents.hub.server.runs.models import Claim, RunSpec
from evo_agents.hub.server.runs.service import overlap
from evo_agents.hub.server.runs.service.claim_rules import claim_query, read_claimer
from evo_agents.hub.server.runs.service.profiles import pick_runtime
from evo_agents.hub.server.runs.service.run_specs import (
    author_skill,
    dressed,
    placed_or_failed,
    run_spec,
    settled_or_failed,
    skill_refusal,
)
from evo_agents.hub.server.security import Principal

log = logging.getLogger("evo_agents.hub.server.runs")  # the package's one logger, as before it was split

CLAIM_POLL_SECONDS = 5.0  # a waiting claim looks again this often, notification or not


class RunWakeups:
    """The claims waiting in this api process, woken by RUNS_CHANNEL on the process's one LISTEN (``listen``),
    opened by the first claim or stream. A notification wakes every waiting claim, which then looks at the queue
    again; a newer claim of a worker wakes only the older claim of that worker, which ends; a claim also looks every
    CLAIM_POLL_SECONDS, so it never depends on the connection being up."""

    def __init__(self, listener: Listener):
        self._listener = listener
        listener.on(RUNS_CHANNEL, lambda payload: self.wake())
        self._event = asyncio.Event()  # set by the next wake-up, then replaced
        self.generation = 0  # counts wake-ups; a claim waits only while it has not changed
        self._tickets: dict[int, int] = {}  # worker id: the number of its newest claim
        self._ends: dict[int, asyncio.Event] = {}  # worker id: set when a newer claim of it ends the waiting one

    def start(self) -> None:
        self._listener.start()

    def wake(self) -> None:
        self.generation += 1
        self._event.set()
        self._event = asyncio.Event()

    async def wait(self, seen: int, timeout: float, worker_id: int, number: int) -> None:
        """Return once something woke the claims after generation ``seen``, a newer claim of ``worker_id`` ended
        claim ``number``, or after ``timeout`` seconds."""
        if self.generation != seen or not self.current(worker_id, number):
            return
        woken = asyncio.ensure_future(self._event.wait())
        ended = asyncio.ensure_future(self._ends[worker_id].wait())
        try:
            await asyncio.wait((woken, ended), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            woken.cancel()
            ended.cancel()

    async def ticket(self, worker_id: int) -> int:
        """The number of a new claim of ``worker_id``, which makes any older one of it end without a run. Only that
        claim wakes: waking every claim here made each new claim of any worker run the claim query of every other."""
        number = self._tickets.get(worker_id, 0) + 1
        self._tickets[worker_id] = number
        older = self._ends.get(worker_id)
        if older is not None:
            older.set()
        self._ends[worker_id] = asyncio.Event()
        return number

    def current(self, worker_id: int, number: int) -> bool:
        return self._tickets.get(worker_id) == number

    def done(self, worker_id: int, number: int) -> None:
        if self._tickets.get(worker_id) == number:
            del self._tickets[worker_id]
            del self._ends[worker_id]


def worker_columns(w=None) -> list:
    """What the claim reads of a worker row of ``w`` (tables.workers by default): id, owner_id, name, slots, runtimes,
    checkouts, drained_at, revoked_at, agent_version, dispatch_from, run_kinds, features, labels, plugins, shared and
    health."""
    w = tables.workers if w is None else w
    return [
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
        w.c.health,
    ]


async def worker_of(conn: AsyncConnection, user: Principal):
    """The row of the worker whose token made the request, locked (``worker_columns``). 403 when there is none."""
    w = tables.workers
    query = select(*worker_columns(w)).where(w.c.token_id == user.token_id).with_for_update()
    row = (await conn.execute(query)).one_or_none()
    if row is None or row.revoked_at is not None:
        raise HTTPException(403, "this worker token belongs to no live worker: join the machine again")
    return row


class ClaimAbandoned(Exception):
    """The worker hung up before its claim was answered; the lease the claim took is rolled back."""

    def __init__(self, run_id: int):
        super().__init__(f"the worker hung up before run {run_id} was handed to it")
        self.run_id = run_id


AGAIN = object()  # a claim whose run its autonomy no longer lets the worker take: the next try looks again
TRIES = 3  # claims in one try, each after one AGAIN


async def _autonomy_at_claim(conn: AsyncConnection, run_id: int, name: str, worker: tuple, asked: str, rule):
    """The autonomy of run ``run_id`` at its claim by the worker ``name`` (``worker``: its environments, features and
    runtimes of the core): the stricter of its snapshot and the value now (``run_autonomy.recheck``, which writes it
    back), None for a run without one, AGAIN when it no longer lets the worker take the run."""
    found = await run_autonomy.recheck(conn, run_id)
    if found is None:
        return None
    environments, features, runtimes = worker
    runtime_ok = run_autonomy.runtime_fits(asked, runtimes, rule)
    why = run_autonomy.claim_fits(found, name, environments, features, runtime_ok)
    if why is None:
        return found
    log.info("claim passed over a run its autonomy keeps from the worker", extra={"run_id": run_id, "why": why})
    return AGAIN


async def _try_claim(
    engine,
    user: Principal,
    lease: timedelta,
    gone: Callable[[], Awaitable[bool]] | None = None,
    blobs=None,
) -> RunSpec | None:
    """``_claim_once``, again while the run it found turned out to be one its autonomy keeps from the worker (whose
    tightened snapshot the next query passes over), up to TRIES times."""
    for _ in range(TRIES):
        spec = await _claim_once(engine, user, lease, gone, blobs)
        if spec is not AGAIN:
            return spec
    return None


async def _claim_once(
    engine,
    user: Principal,
    lease: timedelta,
    gone: Callable[[], Awaitable[bool]] | None = None,
    blobs=None,
):
    """Lease the run the worker of ``user`` may take now, if any, for ``lease`` (see the docstring of the package).
    When ``gone`` says the worker hung up once the run is leased, raise ClaimAbandoned before the transaction
    commits, which rolls the lease back. An author run comes with a presigned GET of its skill from the blob store
    ``blobs``; one the hub cannot hand its skill (none published, or no blob store) fails here, as the reaper, saying
    why, and the claim takes nothing this time."""
    async with engine.begin() as conn:
        found = await worker_of(conn, user)
        worker_id, name, features = found.id, found.name, found.features
        node = await worker_placement.placement(conn, worker_id)
        if not node.ready:  # a node of a pool whose doctor report is stale or has a high finding it does not accept
            return None
        if agent_placement.is_agent_pool(node.pool) and found.drained_at is None:  # the pool is not out of nodes
            await agent_placement.recovered_by(conn, worker_id)
        claimer = await read_claimer(conn, found, node)
        if claimer.stops():
            return None
        runtimes, offered, environments, served = (
            claimer.runtimes,
            claimer.offered,
            claimer.environments,
            claimer.served,
        )
        query = claim_query(claimer.shape())
        waiting = await overlap.blocked(conn, list(served))
        if waiting:  # a step run whose files another run of its plan is editing waits in the queue
            query = query.where(tables.runs.c.id.not_in(waiting))
        row = (await conn.execute(query)).one_or_none()
        if row is None:
            return None
        run_id, asked, kind, rule, project_id, environment_rule, packs = row
        found = await _autonomy_at_claim(conn, run_id, name, (environments, features, runtimes), asked, rule)
        if found is AGAIN:
            return AGAIN
        environment_rule = run_autonomy.environment_rule(environment_rule, found)
        project = served[project_id]
        placed = await placed_or_failed(conn, run_id, asked if asked in offered else None, project_id, project)
        if placed is not None:
            placed = await settled_or_failed(
                conn, run_id, placed, (environment_rule, environments), project_id, (packs, blobs)
            )
        if placed is None:
            return None
        skill = None
        if kind == "author":
            skill = await author_skill(conn)
            refused = skill_refusal(skill, blobs)
            if refused is not None:
                await move_run(conn, run_id, "queued", "failed", "reaper", reason=refused, error=refused)
                log.warning("author run failed at its claim", extra={"run_id": run_id, "worker_id": worker_id})
                return None
        runtime = run_autonomy.claim_runtime(found) or pick_runtime(asked, runtimes, rule)
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
        spec = await dressed(await run_spec(conn, run_id, tuple(features or ())), placed, skill, blobs)
        if gone is not None and await gone():
            raise ClaimAbandoned(run_id)  # leaving the block rolls the transaction back
    log.info("run claimed", extra={"run_id": run_id, "worker_id": worker_id, "runtime": runtime})
    return spec


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
            if not wakeups.current(worker_id, number):  # a newer claim of the worker ended this one
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
            await wakeups.wait(seen, min(remaining, CLAIM_POLL_SECONDS), worker_id, number)
    finally:
        wakeups.done(worker_id, number)
