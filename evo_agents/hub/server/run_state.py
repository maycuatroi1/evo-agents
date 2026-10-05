"""How the hub moves a run, and what each move does to the queue, the run's log and its step in the plan.

Every move goes through ``move_run``: it checks the move against ``runs.TRANSITIONS`` for its actor, changes the row
only while the run is still in the state the caller read (under the caller's row lock), writes the ``state`` event
the hub numbers in the run's log, and then records in the plan what the move means for the step:

- a run that starts (``leased`` to ``running`` or ``interactive``) sets the step ``in_progress`` with the note
  ``run #N on worker W``;
- a run that ends ``done`` sets the step ``done`` with ``done_at`` (UTC date) and the evidence the run holds;
- a run that ends ``failed`` or ``cancelled`` sets the step back to ``pending`` with a note saying why. A ``lost``
  run is not the end of its dispatch: the next attempt is queued at once, and the step is left as it is.

The plan is written as the member who dispatched the run, through PATCH's own write (``plans.apply_patch``: the
item update of ``evo harness step``, with ``if_revision``), and retried up to PLAN_TRIES times when another write
took the revision in between. The write is the step's record, not the run's truth: it runs in a savepoint, so a plan
the dispatcher can no longer write (the writer role gone, the plan deleted or no longer valid) leaves the move in
place and logs a warning. A step that is ``done`` already is never set back.

``release_runs`` is what happens to the runs of a revoked worker, and ``recover_runs`` (the job hub.recover_runs,
every minute) to the runs whose lease ran out: a run whose cancel was asked for is cancelled, the last attempt fails,
and any other run is lost, with its next attempt queued for the same step (``parent_run_id`` pointing back, the
attempt one higher, the runtime the dispatch asked for) and the channel RUNS_CHANNEL notified with the new run's id.
A revoked worker also fails the runs pinned to it, held or queued, since no other worker may claim them; an expired
lease does not, since the worker may come back. ``prune_run_events`` (hub.prune_run_events, daily) deletes the
events of runs that ended more than EVO_HUB_RUN_LOG_DAYS ago.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg
from fastapi import HTTPException
from psycopg import sql
from psycopg.types.json import Jsonb

from evo_agents.hub import runs
from evo_agents.hub.access import has_role
from evo_agents.hub.plans import PlanProblem, step_index
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import MACHINE, Principal

log = logging.getLogger(__name__)

RUNS_CHANNEL = "evo_runs"  # notified with a run's id when it is queued, so a waiting claim looks again
PLAN_TRIES = 5  # writes of a step tried before giving up on revision conflicts
MAX_NOTE_CHARS = 1000  # a note the hub sets on a step
MAX_EVIDENCE_BYTES = 16 * 1024  # runs.evidence, as schema 0009 bounds it
RECOVER_BATCH = 500  # expired runs one pass of the reaper takes
LEASE = timedelta(seconds=runs.LEASE_SECONDS)
# Columns a move may set besides the state, its times and the lease.
MOVE_COLUMNS = frozenset(
    {
        "worker_id",
        "runtime",
        "session_id",
        "commit_sha",
        "diffstat",
        "verify",
        "evidence",
        "usage",
    }
)

MOVE = """
UPDATE runs SET state = %(to)s, error = coalesce(%(error)s, error), event_seq = event_seq + 1,
       leased_at = CASE WHEN %(to)s = 'leased' THEN now() ELSE leased_at END,
       started_at = CASE WHEN %(to)s IN ('running', 'interactive') THEN coalesce(started_at, now())
                         ELSE started_at END,
       lease_expires_at = CASE WHEN %(to)s = ANY(%(held)s) THEN coalesce(lease_expires_at, now() + %(lease)s)
                               ELSE NULL END,
       finished_at = CASE WHEN %(to)s = ANY(%(terminal)s) THEN now() ELSE finished_at END{extra}
 WHERE id = %(id)s AND state = %(from)s
RETURNING event_seq
"""
NEXT_ATTEMPT = """
INSERT INTO runs (project_id, plan_id, step_key, plan_revision, dispatched_by, pinned_worker_id, requested_runtime,
                  runtime, mode, approval, timeout_s, attempt, max_attempts, parent_run_id, repo, branch)
SELECT project_id, plan_id, step_key, plan_revision, dispatched_by, pinned_worker_id, requested_runtime,
       requested_runtime, mode, approval, timeout_s, attempt + 1, max_attempts, id, repo, branch
  FROM runs WHERE id = %s
RETURNING id
"""
HELD_RUNS = """
SELECT id, state, attempt, max_attempts, pinned_worker_id, cancel_requested_at IS NOT NULL
  FROM runs
 WHERE worker_id = %s AND state = ANY(%s)
 ORDER BY id
   FOR UPDATE
"""
PINNED_QUEUED = "SELECT id, state FROM runs WHERE pinned_worker_id = %s AND state = 'queued' ORDER BY id FOR UPDATE"
EXPIRED = "SELECT id FROM runs WHERE state = ANY(%s) AND lease_expires_at < now() ORDER BY lease_expires_at LIMIT %s"
LOCK_EXPIRED = """
SELECT r.id, r.state, r.attempt, r.max_attempts, r.cancel_requested_at IS NOT NULL, w.name
  FROM runs r LEFT JOIN workers w ON w.id = r.worker_id
 WHERE r.id = %s AND r.state = ANY(%s) AND r.lease_expires_at < now()
   FOR UPDATE OF r SKIP LOCKED
"""
UNCLAIMABLE = """
SELECT r.id, w.name
  FROM runs r JOIN workers w ON w.id = r.pinned_worker_id
 WHERE r.state = 'queued' AND w.revoked_at IS NOT NULL
 ORDER BY r.id
 LIMIT %s
   FOR UPDATE OF r SKIP LOCKED
"""
PRUNE_EVENTS = """
DELETE FROM run_events e USING runs r
 WHERE e.run_id = r.id AND r.finished_at IS NOT NULL AND r.finished_at < now() - make_interval(days => %s)
"""
RUN_STEP = """
SELECT r.id, p.name, r.plan_id, r.step_key, r.dispatched_by, u.login, w.name, r.runtime, r.attempt, r.max_attempts,
       r.repo, r.branch, r.commit_sha, r.diffstat, r.verify, r.evidence, r.error
  FROM runs r JOIN projects p ON p.id = r.project_id JOIN users u ON u.id = r.dispatched_by
  LEFT JOIN workers w ON w.id = r.worker_id
 WHERE r.id = %s
"""


async def notify_queued(conn, run_id: int) -> None:
    """Wake the claims waiting on RUNS_CHANNEL once the caller's transaction commits."""
    await conn.execute("SELECT pg_notify(%s, %s)", (RUNS_CHANNEL, str(run_id)))


def _column_value(value):
    return Jsonb(value) if isinstance(value, (dict, list)) else value


async def move_run(
    conn,
    run_id: int,
    old: str,
    new: str,
    actor: str,
    *,
    reason: str,
    error: str | None = None,
    columns: dict | None = None,
    token_id: int | None = None,
) -> int | None:
    """Move run ``run_id``, whose row the caller holds locked, from ``old`` to ``new`` as ``actor``, setting
    ``columns`` (of MOVE_COLUMNS) too; write the ``state`` event, then record the move in the plan (see the module's
    docstring), as the dispatcher with ``token_id`` in the audit row. Returns the plan revision the move wrote, or
    None. Raises ``runs.TransitionRefused`` for a move the table refuses or a run that is no longer in ``old``."""
    runs.check_transition(old, new, actor)
    columns = dict(columns or {})
    unknown = set(columns) - MOVE_COLUMNS
    if unknown:
        raise ValueError(f"a move cannot set {', '.join(sorted(unknown))}")
    extra = sql.SQL("").join(
        sql.SQL(", {} = {}").format(sql.Identifier(name), sql.Placeholder(f"set_{name}")) for name in columns
    )
    params = {
        "id": run_id,
        "from": old,
        "to": new,
        "error": error,
        "held": list(runs.HELD_STATES),
        "terminal": list(runs.TERMINAL_STATES),
        "lease": LEASE,
        **{f"set_{name}": _column_value(value) for name, value in columns.items()},
    }
    row = await (await conn.execute(sql.SQL(MOVE).format(extra=extra), params)).fetchone()
    if row is None:
        raise runs.TransitionRefused(f"run {run_id} is no longer {old}: it moved meanwhile")
    body = {"from": old, "to": new, "actor": actor, "reason": reason}
    await conn.execute(
        "INSERT INTO run_events (run_id, seq, kind, body) VALUES (%s, %s, 'state', %s)", (run_id, row[0], Jsonb(body))
    )
    return await record_move(conn, run_id, old, new, reason=reason, token_id=token_id)


# The plan


@dataclass(frozen=True)
class RunStep:
    """What a plan write needs to know of a run."""

    run_id: int
    project: str
    plan_id: str
    step_key: str
    dispatcher_id: int
    dispatcher: str
    worker: str | None
    runtime: str
    attempt: int
    max_attempts: int
    repo: str
    branch: str | None
    commit_sha: str | None
    diffstat: dict | None
    verify: list | None
    evidence: str | None
    error: str | None


async def run_step(conn, run_id: int) -> RunStep:
    row = await (await conn.execute(RUN_STEP, (run_id,))).fetchone()
    return RunStep(*row)


def _note(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_NOTE_CHARS else text[: MAX_NOTE_CHARS - 3] + "..."


def evidence(found: RunStep, summary: str | None = None) -> str:
    """The evidence a run gives its step: the run, the commit and its diffstat, each verify command with its exit
    code, and the agent's summary, within MAX_EVIDENCE_BYTES."""
    head = f"run #{found.run_id} on worker {found.worker or '?'} ({found.runtime}, attempt {found.attempt})"
    if found.commit_sha:
        head += f": {found.repo}@{found.commit_sha[:12]}"
        if found.branch:
            head += f" on {found.branch}"
    stat = found.diffstat or {}
    if all(isinstance(stat.get(key), int) for key in ("files", "insertions", "deletions")):
        head += f", {stat['files']} files +{stat['insertions']} -{stat['deletions']}"
    lines = [head + "."]
    checks = [
        f"`{' '.join(str(item.get('command', '')).split())}` exit {item.get('exit_code')}"
        for item in found.verify or []
        if isinstance(item, dict)
    ]
    if checks:
        lines.append("verify: " + "; ".join(checks) + ".")
    if summary and summary.strip():
        lines.append(summary.strip())
    return runs.clip("\n".join(lines), MAX_EVIDENCE_BYTES)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def step_updates(found: RunStep, old: str, new: str, reason: str):
    """A function from the step as the plan holds it to the keys the move sets on it (None: leave the step alone),
    or None when the move means nothing for the plan."""
    if old == "leased" and new in ("running", "interactive"):
        updates = {"status": "in_progress", "note": _note(f"run #{found.run_id} on worker {found.worker}")}
    elif new == "done":
        updates = {"status": "done", "done_at": _today(), "evidence": found.evidence or evidence(found)}
    elif new == "failed":
        updates = {"status": "pending", "note": _note(f"run #{found.run_id} failed: {found.error or reason}")}
    elif new == "cancelled":
        updates = {"status": "pending", "note": _note(f"run #{found.run_id} was cancelled: {reason}")}
    else:
        return None
    return lambda step: None if step.get("status") == "done" else updates


async def read_plan(conn, access, plan_id: str):
    """The plan as held, without a lock: the revision the write is made against."""
    return await plan_routes._held(conn, access, plan_id)


class _Skipped(Exception):
    pass


async def write_step(conn, found: RunStep, updates_for, *, token_id: int | None = None) -> int | None:
    """Set on the run's step what ``updates_for`` gives for it, as the dispatcher, in a savepoint of the caller's
    transaction; the new revision, or None when nothing was written."""
    actor = Principal(found.dispatcher_id, found.dispatcher, False, token_id, MACHINE, "")
    where = {"run_id": found.run_id, "project": found.project, "plan_id": found.plan_id, "step": found.step_key}
    try:
        async with conn.transaction():  # a savepoint: a refused write leaves the move in place
            access = await project_access(conn, actor, found.project)
            if not has_role(access.role, "writer"):
                raise _Skipped(f"{found.dispatcher} no longer holds the writer role on {found.project}")
            for attempt in range(1, PLAN_TRIES + 1):
                held = await read_plan(conn, access, found.plan_id)
                if held is None:
                    raise _Skipped("the plan is not on the hub any more")
                steps = held.body.get("steps")
                step = steps[step_index(held.body, found.step_key)]
                if not isinstance(step, dict):
                    raise _Skipped("the step is a bare string")
                updates = updates_for(step)
                if updates is None:
                    return None
                try:
                    # A savepoint per try: a conflict rolls it back, which frees the plan's row for the other
                    # writer until the next try reads the plan again.
                    async with conn.transaction():
                        stored, changed, _ = await plan_routes.apply_patch(
                            conn,
                            access,
                            actor,
                            found.plan_id,
                            "steps",
                            updates,
                            if_revision=held.revision,
                            step=found.step_key,
                        )
                except plan_routes.PlanError as exc:
                    if exc.code != plan_routes.REVISION_CONFLICT or attempt == PLAN_TRIES:
                        raise
                    log.info("plan revision conflict on a run's step; trying again", extra={**where, "try": attempt})
                    continue
                return stored.revision if changed else None
    except _Skipped as exc:
        log.warning("run step not written to the plan", extra={**where, "why": str(exc)})
    except HTTPException as exc:
        log.warning("run step not written to the plan", extra={**where, "status": exc.status_code, "why": exc.detail})
    except plan_routes.PlanError as exc:
        log.warning("run step not written to the plan", extra={**where, "status": exc.status, "why": exc.message})
    except PlanProblem as exc:
        log.warning("run step not written to the plan", extra={**where, "why": str(exc)})
    except psycopg.Error as exc:
        log.error("run step not written to the plan", extra={**where, "error": f"{type(exc).__name__}: {exc}"})
    return None


async def record_move(conn, run_id: int, old: str, new: str, *, reason: str, token_id: int | None = None) -> int | None:
    """Record in the plan what moving run ``run_id`` from ``old`` to ``new`` means for its step."""
    found = await run_step(conn, run_id)
    updates_for = step_updates(found, old, new, reason)
    if updates_for is None:
        return None
    return await write_step(conn, found, updates_for, token_id=token_id)


# Runs that lose their worker


async def end_held(
    conn,
    run_id: int,
    state: str,
    *,
    attempt: int,
    max_attempts: int,
    cancel: bool,
    pinned_here: bool,
    reason: str,
) -> str:
    """End a held run whose worker will not extend its lease again, as the reaper, in the caller's transaction:
    cancelled when its cancel was asked for, failed when it is pinned to that worker (``pinned_here``) or on its last
    attempt, and otherwise lost with the next attempt queued. Returns the state it ended in."""
    if cancel:
        await move_run(conn, run_id, state, "cancelled", "reaper", reason=reason)
        return "cancelled"
    if pinned_here or attempt >= max_attempts:
        why = "the run was pinned to it" if pinned_here else f"it was attempt {attempt} of {max_attempts}"
        await move_run(conn, run_id, state, "failed", "reaper", reason=reason, error=f"{reason}, and {why}")
        return "failed"
    await move_run(conn, run_id, state, "lost", "reaper", reason=reason, error=reason)
    next_id = (await (await conn.execute(NEXT_ATTEMPT, (run_id,))).fetchone())[0]
    await notify_queued(conn, next_id)
    return "lost"


async def release_runs(conn, worker_id: int, reason: str) -> int:
    """What the reaper does to the runs of a worker that will never extend a lease again, in the caller's
    transaction: each run it holds is cancelled when its cancel was asked for, fails when it is pinned to this worker
    or on its last attempt, and is otherwise lost, with the next attempt queued for the same step. Queued runs pinned
    to the worker fail, since no other worker may claim them. Returns how many runs were moved."""
    moved = 0
    for run_id, state, attempt, max_attempts, pinned, cancel in await (
        await conn.execute(HELD_RUNS, (worker_id, list(runs.HELD_STATES)))
    ).fetchall():
        await end_held(
            conn,
            run_id,
            state,
            attempt=attempt,
            max_attempts=max_attempts,
            cancel=cancel,
            pinned_here=pinned == worker_id,
            reason=reason,
        )
        moved += 1
    for run_id, state in await (await conn.execute(PINNED_QUEUED, (worker_id,))).fetchall():
        error = f"{reason}, and the run was pinned to it"
        await move_run(conn, run_id, state, "failed", "reaper", reason=reason, error=error)
        moved += 1
    return moved


async def recover_runs(pool, batch: int = RECOVER_BATCH) -> dict:
    """One pass of the reaper: every held run whose lease ran out ends as ``end_held`` says, each in a transaction of
    its own, and a queued run pinned to a revoked worker fails. Returns how many runs ended in each state."""
    ended: Counter[str] = Counter()
    async with pool.connection() as conn:
        expired = [row[0] for row in await (await conn.execute(EXPIRED, (list(runs.HELD_STATES), batch))).fetchall()]
    for run_id in expired:
        async with pool.connection() as conn:
            row = await (await conn.execute(LOCK_EXPIRED, (run_id, list(runs.HELD_STATES)))).fetchone()
            if row is None:  # a heartbeat extended it, or another pass took it
                continue
            _, state, attempt, max_attempts, cancel, worker = row
            reason = f"its worker {worker} stopped extending the lease"
            ended[
                await end_held(
                    conn,
                    run_id,
                    state,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    cancel=cancel,
                    pinned_here=False,
                    reason=reason,
                )
            ] += 1
    async with pool.connection() as conn:
        for run_id, worker in await (await conn.execute(UNCLAIMABLE, (batch,))).fetchall():
            reason = f"its pinned worker {worker} was revoked"
            error = f"{reason}, so no worker may claim it"
            await move_run(conn, run_id, "queued", "failed", "reaper", reason=reason, error=error)
            ended["failed"] += 1
    report = {state: ended.get(state, 0) for state in ("lost", "failed", "cancelled")}
    if expired or ended:
        log.warning("runs recovered", extra=report)
    return report


async def prune_run_events(pool, days: int) -> dict:
    """Delete the events of runs that ended more than ``days`` days ago."""
    async with pool.connection() as conn:
        deleted = (await conn.execute(PRUNE_EVENTS, (days,))).rowcount
    log.info("run events pruned", extra={"deleted": deleted, "days": days})
    return {"deleted": deleted, "days": days}
