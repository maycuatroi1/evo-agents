"""How the hub moves a run, and what each move does to the queue, the run's log and its step in the plan.

Every move goes through ``move_run``: it checks the move against ``runs.TRANSITIONS`` for its actor, changes the row
only while the run is still in the state the caller read (under the caller's row lock), writes the ``state`` event
the hub numbers in the run's log, and then records in the plan what the move means for the step:

- a run that starts (``leased`` to ``running`` or ``interactive``) sets the step ``in_progress`` with the note
  ``run #N on worker W``;
- a run that ends ``done`` sets the step ``done`` with ``done_at`` (UTC date) and the evidence the run holds;
- a run that ends ``failed`` or ``cancelled`` sets the step back to ``pending`` with a note saying why. A ``lost``
  run is not the end of its dispatch: the next attempt is queued at once, and the step is left as it is.

A plan run has no step of its own: its worker reports each step (POST /v1/worker/runs/{id}/steps/{key}, which writes
it through ``write_step``), and the hub keeps each report as a ``system`` event of the run with a ``step_report`` key.
Its moves write no step, except that a plan run that ends ``failed`` or ``cancelled`` sets back to ``pending``, with
the same notes, the steps that it, or an earlier attempt or run it went on from, reported and that are still
``in_progress``. A plan run that ends ``done`` writes nothing.

The plan is written as the member who dispatched the run, through PATCH's own write (``plans.apply_patch``: the
item update of ``evo harness step``, with ``if_revision``), and retried up to PLAN_TRIES times when another write
took the revision in between. The write is the step's record, not the run's truth: it runs in a savepoint, so a plan
the dispatcher can no longer write (the writer role gone, the plan deleted or no longer valid) leaves the move in
place and logs a warning. A step that is ``done`` already is never set back.

A move also drops the owner's ask for a takeover once the run leaves leased and running, and for a handback once it
leaves interactive, and notifies EVENTS_CHANNEL with the run's id, as every write of a run's events does, so the
streams of the run (``run_events``) send the new event at once. It settles the run's agent time: ``run_seconds`` takes
the whole seconds since ``counted_at`` (else since the agent started, else since the claim) while the run was in one
of ``runs.CLOCK_STATES``, and ``counted_at`` moves on to where the count stopped, or is cleared when the run leaves
those states; a heartbeat settles it the same way (``settle``). A move to ``waiting`` sets ``waiting_since`` (any other
move clears it) and one to ``parked`` sets ``parked_at``. A run that ends cancels its decisions still open, unless the
caller deals with them (``decisions=None``): the reaper lets those of a run parked too long expire, and an answer to a
parked run hands them to the run that resumes it. A plan run that ends ``failed`` sends its owner the notice
``run_failed``, and one that ends ``done`` with every step of its plan done the notice ``plan_finished``
(``notifications.notify``); one that ends ``done`` because a new run resumes it sends nothing.

``release_runs`` is what happens to the runs of a revoked worker, and ``recover_runs`` (the job hub.recover_runs,
every minute) to the held runs that ran past their timeout (their agent time, ``run_seconds`` and the time since it was
last settled, over ``timeout_s``: waiting for an answer and being parked do not count), to the runs whose lease ran
out, to the runs that waited too long for an answer and to those parked too long. A run past its timeout fails, or is
cancelled when its cancel was asked for, and is not tried again. Of the runs whose lease ran out, one whose cancel was
asked for is cancelled, the last attempt fails, and any other run is lost, with its next attempt queued for the same
step (``parent_run_id`` pointing back, the attempt one higher, the runtime the dispatch asked for) and the channel
RUNS_CHANNEL notified with the new run's id. A run ``waiting`` for DECISION_WAIT is ``parked`` (cancelled instead
when its cancel was asked for): the worker's next heartbeat says park, and the slot is free. A run ``parked`` for
PARKED_FOR is cancelled and its open decisions expire. A revoked worker also fails the runs pinned to it, held or
queued, since no other worker may claim them, and cancels the runs parked on it, since only it has their session; an
expired lease does not, since the worker may come back. ``prune_run_events`` (hub.prune_run_events, daily)
deletes the events of runs that ended more than EVO_HUB_RUN_LOG_DAYS ago, and the sealed GitHub tokens past their end.

A run that leaves the held states (it ends, waits in review, or is parked) gives back its credentials: ``move_run``
marks its leases revoked in the same transaction (``credentials.end_leases``), and the GitHub tokens among them are
revoked at GitHub once it commits, by the route that moved it and by every pass of the reaper
(``credentials.revoke_tokens``).
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from fastapi import HTTPException
from sqlalchemy import Integer, case, cast, delete, extract, func, insert, null, or_, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.plans import PlanProblem, step_index
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import MACHINE, Principal

if TYPE_CHECKING:
    from evo_agents.hub.server.github_app import GitHubApp
    from evo_agents.hub.server.sealing import Sealer

log = logging.getLogger(__name__)

RUNS_CHANNEL = "evo_runs"  # notified with a run's id when it is queued, so a waiting claim looks again
EVENTS_CHANNEL = "evo_run_events"  # notified with a run's id when an event of it is written, so its streams look again
PLAN_TRIES = 5  # writes of a step tried before giving up on revision conflicts
MAX_NOTE_CHARS = 1000  # a note the hub sets on a step
MAX_EVIDENCE_BYTES = 16 * 1024  # runs.evidence, as schema 0009 bounds it
RECOVER_BATCH = 500  # expired runs one pass of the reaper takes
LEASE = timedelta(seconds=runs.LEASE_SECONDS)  # the api's own comes from EVO_HUB_RUN_LEASE_SECONDS
DECISION_WAIT = timedelta(seconds=runs.DECISION_WAIT_SECONDS)  # a run waiting this long for an answer is parked
PARKED_FOR = timedelta(days=runs.PARKED_DAYS)  # a run parked this long is cancelled
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


# Agent time, in an UPDATE of runs (whose right-hand sides read the row as it was) or a query of it.


def _clock_from():
    """When the count last stopped: counted_at, else when the agent started, else the claim."""
    r = tables.runs
    return func.coalesce(r.c.counted_at, r.c.started_at, r.c.leased_at, func.now())


def _elapsed():
    """The whole seconds since the count last stopped while the run is in one of runs.CLOCK_STATES, else 0."""
    r = tables.runs
    since = func.greatest(0, func.floor(extract("epoch", func.now() - _clock_from())))
    return case((r.c.state.in_(runs.CLOCK_STATES), cast(since, Integer)), else_=0)


def _seconds(count):
    """``make_interval(secs => count)``: make_interval's seventh argument is the seconds."""
    return func.make_interval(0, 0, 0, 0, 0, 0, count)


def settle() -> dict:
    """What a heartbeat sets to settle a run's agent time without moving it, as values of an update of runs; the
    fraction of a second left goes on counting."""
    r = tables.runs
    return {
        "run_seconds": r.c.run_seconds + _elapsed(),
        "counted_at": case((r.c.state.in_(runs.CLOCK_STATES), _clock_from() + _seconds(_elapsed()))),
    }


def _past_timeout():
    r = tables.runs
    return r.c.run_seconds + _elapsed() > r.c.timeout_s


def _column_value(value):
    """A value a move sets: None is SQL NULL, also in a JSONB column, which would store a JSON null for it."""
    return null() if value is None else value


def _move(run_id: int, old: str, new: str, error: str | None, columns: dict, lease: timedelta):
    """The UPDATE of a move: ``new`` is known here, so what it sets is decided in Python; the right-hand sides that
    read the row (the agent time) are SQL and read it as it was."""
    r = tables.runs
    values = {
        "state": new,
        "event_seq": r.c.event_seq + 1,
        "run_seconds": r.c.run_seconds + _elapsed(),
        "waiting_since": func.now() if new == "waiting" else None,
    }
    if error is not None:
        values["error"] = error
    if new == "leased":
        values["leased_at"] = func.now()
    if new in ("running", "interactive"):
        values["started_at"] = func.coalesce(r.c.started_at, func.now())
    held = new in runs.HELD_STATES
    values["lease_expires_at"] = func.coalesce(r.c.lease_expires_at, func.now() + lease) if held else None
    if new in runs.TERMINAL_STATES:
        values["finished_at"] = func.now()
    if new in runs.CLOCK_STATES:
        in_clock = r.c.state.in_(runs.CLOCK_STATES)
        values["counted_at"] = case((in_clock, _clock_from() + _seconds(_elapsed())), else_=func.now())
    else:
        values["counted_at"] = None
    if new == "parked":
        values["parked_at"] = func.now()
    if new not in runs.TAKEOVER_STATES:
        values["takeover_requested_at"] = None
    if new not in runs.HANDBACK_STATES:
        values["handback_requested_at"] = None
    values |= {name: _column_value(value) for name, value in columns.items()}
    return update(r).values(**values).where(r.c.id == run_id, r.c.state == old).returning(r.c.event_seq)


async def _insert_event(conn: AsyncConnection, run_id: int, seq: int, kind: str, body: dict) -> None:
    await conn.execute(insert(tables.run_events).values(run_id=run_id, seq=seq, kind=kind, body=body))


async def notify_queued(conn: AsyncConnection, run_id: int) -> None:
    """Wake the claims waiting on RUNS_CHANNEL once the caller's transaction commits."""
    await conn.execute(select(func.pg_notify(RUNS_CHANNEL, str(run_id))))


async def notify_events(conn: AsyncConnection, run_id: int) -> None:
    """Wake the streams of run ``run_id`` on EVENTS_CHANNEL once the caller's transaction commits; the notifications
    of one transaction with the same run arrive as one."""
    await conn.execute(select(func.pg_notify(EVENTS_CHANNEL, str(run_id))))


async def move_run(
    conn: AsyncConnection,
    run_id: int,
    old: str,
    new: str,
    actor: str,
    *,
    reason: str,
    error: str | None = None,
    columns: dict | None = None,
    token_id: int | None = None,
    lease: timedelta = LEASE,
    decisions: str | None = "cancelled",
) -> int | None:
    """Move run ``run_id``, whose row the caller holds locked, from ``old`` to ``new`` as ``actor``, setting
    ``columns`` (of MOVE_COLUMNS) too; write the ``state`` event, then record the move in the plan (see the module's
    docstring), as the dispatcher with ``token_id`` in the audit row. A run that comes to be held without a lease
    (the claim) is leased for ``lease``. A run that ends puts its decisions still open in the state ``decisions``, or
    leaves them open when it is None. Returns the plan revision the move wrote, or None. Raises
    ``runs.TransitionRefused`` for a move the table refuses or a run that is no longer in ``old``."""
    runs.check_transition(old, new, actor)
    columns = dict(columns or {})
    unknown = set(columns) - MOVE_COLUMNS
    if unknown:
        raise ValueError(f"a move cannot set {', '.join(sorted(unknown))}")
    seq = (await conn.execute(_move(run_id, old, new, error, columns, lease))).scalar_one_or_none()
    if seq is None:
        raise runs.TransitionRefused(f"run {run_id} is no longer {old}: it moved meanwhile")
    await _insert_event(conn, run_id, seq, "state", {"from": old, "to": new, "actor": actor, "reason": reason})
    if new in runs.TERMINAL_STATES and decisions is not None:
        d = tables.decisions
        await conn.execute(update(d).values(state=decisions).where(d.c.run_id == run_id, d.c.state == "open"))
    if new not in runs.HELD_STATES:  # no worker holds it now: the hub takes back what it leased the run
        from evo_agents.hub.server import credentials  # its routes read runs through the routes that import this module

        await credentials.end_leases(conn, run_id=run_id, by=f"run-{new}")
    await notify_events(conn, run_id)
    return await record_move(conn, run_id, old, new, reason=reason, token_id=token_id)


# The plan


@dataclass(frozen=True)
class RunStep:
    """What a plan write needs to know of a run."""

    run_id: int
    project: str
    plan_id: str
    step_key: str | None  # None for a plan run, whose writes name the step they are for
    dispatcher_id: int
    dispatcher: str
    worker: str | None
    runtime: str
    attempt: int
    max_attempts: int
    repo: str | None  # None for a plan run
    branch: str | None
    commit_sha: str | None
    diffstat: dict | None
    verify: list | None
    evidence: str | None
    error: str | None
    kind: str = "step"
    repos: list | None = None  # a plan run's repos, each {"repo", "branch"}
    project_id: int | None = None


def _run_step(run_id: int):
    r, p, u, w = tables.runs, tables.projects, tables.users, tables.workers
    return (
        select(
            r.c.id.label("run_id"),
            p.c.name.label("project"),
            r.c.plan_id,
            r.c.step_key,
            r.c.dispatched_by.label("dispatcher_id"),
            u.c.login.label("dispatcher"),
            w.c.name.label("worker"),
            r.c.runtime,
            r.c.attempt,
            r.c.max_attempts,
            r.c.repo,
            r.c.branch,
            r.c.commit_sha,
            r.c.diffstat,
            r.c.verify,
            r.c.evidence,
            r.c.error,
            r.c.kind,
            r.c.repos,
            r.c.project_id,
        )
        .select_from(
            r.join(p, p.c.id == r.c.project_id)
            .join(u, u.c.id == r.c.dispatched_by)
            .outerjoin(w, w.c.id == r.c.worker_id)
        )
        .where(r.c.id == run_id)
    )


async def run_step(conn: AsyncConnection, run_id: int) -> RunStep:
    row = (await conn.execute(_run_step(run_id))).one()
    return RunStep(**row._mapping)


async def next_seq(conn: AsyncConnection, run_id: int) -> int:
    """The hub's next number in the log of run ``run_id``, whose row the caller holds locked, counted on the run."""
    r = tables.runs
    stmt = update(r).values(event_seq=r.c.event_seq + 1).where(r.c.id == run_id).returning(r.c.event_seq)
    return (await conn.execute(stmt)).scalar_one()


async def write_event(conn: AsyncConnection, run_id: int, body: dict) -> int:
    """Write a ``system`` event of the hub's own into the log of run ``run_id``, whose row the caller holds locked,
    and wake its streams; its seq. Like the ``state`` events, it is written past the run's limit of events."""
    seq = await next_seq(conn, run_id)
    await _insert_event(conn, run_id, seq, "system", body)
    await notify_events(conn, run_id)
    return seq


def _note(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_NOTE_CHARS else text[: MAX_NOTE_CHARS - 3] + "..."


def evidence(found: RunStep, summary: str | None = None) -> str:
    """The evidence a run gives its step: the run, the commit and its diffstat, each verify command with its exit
    code, and the agent's summary, within MAX_EVIDENCE_BYTES."""
    head = f"run #{found.run_id} on worker {found.worker or '?'} ({found.runtime}, attempt {found.attempt})"
    if found.commit_sha and found.repo:
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
    return datetime.now(UTC).date().isoformat()


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


def report_updates(found: RunStep, status: str, summary: str | None) -> dict:
    """The keys a plan run's report of step ``found.step_key`` sets on it: ``in_progress`` with the note a run of one
    step sets when it starts, ``pending`` with a note that the run handed it back, or ``done`` with ``done_at``; and the
    evidence (``evidence``, the agent's ``summary`` last) when the step is done or the report carries evidence."""
    on = f"run #{found.run_id} on worker {found.worker}"
    if status == "in_progress":
        updates = {"status": "in_progress", "note": _note(on)}
    elif status == "pending":
        updates = {"status": "pending", "note": _note(f"{on} handed it back")}
    else:
        updates = {"status": "done", "done_at": _today()}
    if status == "done" or (summary and summary.strip()):
        updates["evidence"] = evidence(found, summary)
    return updates


async def read_plan(conn, access, plan_id: str):
    """The plan as held, without a lock: the revision the write is made against."""
    return await plan_routes._held(conn, access, plan_id)


class _Skipped(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class StepNotWritten(Exception):
    """A strict ``write_step`` could not write the step: ``status`` and ``message`` are the HTTP answer."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


async def write_step(
    conn: AsyncConnection, found: RunStep, updates_for, *, token_id: int | None = None, strict: bool = False
) -> int | None:
    """Set on the run's step what ``updates_for`` gives for it, as the dispatcher, in a savepoint of the caller's
    transaction; the new revision, or None when nothing was written. A write the plan refuses is logged and skipped,
    or, when ``strict``, raised as StepNotWritten (an HTTPException ``updates_for`` raises goes up as it is)."""
    actor = Principal(found.dispatcher_id, found.dispatcher, False, token_id, MACHINE, "")
    where = {"run_id": found.run_id, "project": found.project, "plan_id": found.plan_id, "step": found.step_key}
    try:
        async with conn.begin_nested():  # a savepoint: a refused write leaves the move in place
            access = await project_access(conn, actor, found.project)
            if not has_role(access.role, "writer"):
                raise _Skipped(403, f"{found.dispatcher} no longer holds the writer role on {found.project}")
            for attempt in range(1, PLAN_TRIES + 1):
                held = await read_plan(conn, access, found.plan_id)
                if held is None:
                    raise _Skipped(404, "the plan is not on the hub any more")
                steps = held.body.get("steps")
                step = steps[step_index(held.body, found.step_key)]
                if not isinstance(step, dict):
                    raise _Skipped(422, "the step is a bare string, which has no status to set")
                updates = updates_for(step)
                if updates is None:
                    return None
                try:
                    # A savepoint per try: a conflict rolls it back, which frees the plan's row for the other
                    # writer until the next try reads the plan again.
                    async with conn.begin_nested():
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
        refused = StepNotWritten(exc.status, str(exc))
    except HTTPException as exc:
        if strict:
            raise
        log.warning("run step not written to the plan", extra={**where, "status": exc.status_code, "why": exc.detail})
        return None
    except plan_routes.PlanError as exc:
        log.warning("run step not written to the plan", extra={**where, "status": exc.status, "why": exc.message})
        refused = StepNotWritten(exc.status, exc.message)
    except PlanProblem as exc:
        log.warning("run step not written to the plan", extra={**where, "why": str(exc)})
        refused = StepNotWritten(422, str(exc))
    except DBAPIError as exc:  # the database refused or went away; Core wraps the driver's error
        if strict:
            raise
        log.error("run step not written to the plan", extra={**where, "error": f"{type(exc).__name__}: {exc}"})
        return None
    if strict:
        raise refused
    return None


async def record_move(
    conn: AsyncConnection, run_id: int, old: str, new: str, *, reason: str, token_id: int | None = None
) -> int | None:
    """Record in the plan what moving run ``run_id`` from ``old`` to ``new`` means for its step, or for the steps a
    plan run left in progress."""
    found = await run_step(conn, run_id)
    if found.kind == "plan":
        revision = await release_plan_steps(conn, found, new, reason=reason, token_id=token_id)
        await notify_plan_run_end(conn, found, old, new, reason=reason)
        return revision
    updates_for = step_updates(found, old, new, reason)
    if updates_for is None:
        return None
    return await write_step(conn, found, updates_for, token_id=token_id)


async def release_plan_steps(
    conn: AsyncConnection, found: RunStep, new: str, *, reason: str, token_id: int | None = None
) -> int | None:
    """When plan run ``found`` ends ``failed`` or ``cancelled``, set back to ``pending`` each step it reported (it, or
    an earlier attempt or parked run it went on from) that is still ``in_progress``, with the note a run of one step
    leaves; the last revision written, or None. Any other move writes nothing."""
    if new == "failed":
        note = _note(f"run #{found.run_id} failed: {found.error or reason}")
    elif new == "cancelled":
        note = _note(f"run #{found.run_id} was cancelled: {reason}")
    else:
        return None

    def back(step):
        return {"status": "pending", "note": note} if step.get("status") == "in_progress" else None

    keys = await reported_steps(conn, found.run_id)
    revision = None
    for key in sorted(keys, key=_natural):
        written = await write_step(conn, replace(found, step_key=key), back, token_id=token_id)
        revision = written or revision
    return revision


def _natural(key: str) -> tuple:
    """Step keys in the order people number them: 2 before 10, numbers before names."""
    return (0, int(key), "") if key.isascii() and key.isdigit() else (1, 0, key)


def _reported_steps(run_id: int):
    """The steps a plan run reported, its earlier attempts and the parked runs it went on from included, in no
    order: a recursive CTE walks parent_run_id and resume_of_run_id (UNION, so a cycle ends), and the ``system``
    events of the chain whose body holds a ``step_report`` object with a string ``step`` name them."""
    r, e = tables.runs, tables.run_events
    chain = select(r.c.id, r.c.parent_run_id, r.c.resume_of_run_id).where(r.c.id == run_id).cte("chain", recursive=True)
    earlier = tables.runs.alias("r")
    chain = chain.union(
        select(earlier.c.id, earlier.c.parent_run_id, earlier.c.resume_of_run_id).join_from(
            earlier,
            chain,
            or_(earlier.c.id == chain.c.parent_run_id, earlier.c.id == chain.c.resume_of_run_id),
        )
    )
    report = e.c.body["step_report"]
    return (
        select(report["step"].astext)
        .distinct()
        .join_from(e, chain, chain.c.id == e.c.run_id)
        .where(
            e.c.kind == "system",
            func.jsonb_typeof(report) == "object",
            func.jsonb_typeof(report["step"]) == "string",
        )
    )


async def reported_steps(conn: AsyncConnection, run_id: int) -> list[str]:
    """The steps plan run ``run_id`` reported, it or an earlier attempt or parked run it went on from, in the order
    people number them."""
    keys = (await conn.execute(_reported_steps(run_id))).scalars().all()
    return sorted(keys, key=_natural)


def _every_step_done(body) -> bool:
    steps = body.get("steps") if isinstance(body, dict) else None
    if not isinstance(steps, list) or not steps:
        return False
    return all(isinstance(step, dict) and step.get("status") == "done" for step in steps)


async def notify_plan_run_end(conn: AsyncConnection, found: RunStep, old: str, new: str, *, reason: str) -> int | None:
    """Send the owner of plan run ``found`` the notice its end calls for: ``run_failed`` when it failed (its last
    attempt lost included, which fails), ``plan_finished`` when it ended done with every step of its plan done. A run
    done because a new run resumes it, and any other move, send nothing. The notification's id, or None."""
    from evo_agents.hub.server import notifications  # it reads runs through the routes that import this module

    link = notifications.run_link(found.project, found.run_id)
    if new == "failed":
        error = found.error or reason
        return await notifications.notify(
            conn,
            user_id=found.dispatcher_id,
            kind="notice",
            notice_kind="run_failed",
            project_id=found.project_id,
            run_id=found.run_id,
            title=f"Plan run #{found.run_id} of {found.plan_id} failed",
            body=error,
            details={"plan_id": found.plan_id, "error": error},
            link=link,
        )
    if new != "done" or old == "parked":
        return None
    pl = tables.plans
    body = (
        await conn.execute(select(pl.c.body).where(pl.c.project_id == found.project_id, pl.c.plan_id == found.plan_id))
    ).scalar_one_or_none()
    if body is None or not _every_step_done(body):
        return None
    steps = await reported_steps(conn, found.run_id)
    done = f"steps {', '.join(steps)}" if steps else "no step of its own"
    return await notifications.notify(
        conn,
        user_id=found.dispatcher_id,
        kind="notice",
        notice_kind="plan_finished",
        project_id=found.project_id,
        run_id=found.run_id,
        title=f"Plan {found.plan_id} finished",
        body=f"Plan run #{found.run_id} ended with every step of the plan done; it did {done}.",
        details={"plan_id": found.plan_id, "steps": steps},
        link=link,
    )


# Runs that lose their worker


def _next_attempt(run_id: int):
    """The next attempt of run ``run_id``: the same step, ``parent_run_id`` pointing back, the attempt one higher and
    the runtime the dispatch asked for. It keeps the dispatch's credential, so a worker that takes runs dispatched
    from the web only takes the retry of one too, and never the retry of a run dispatched with a token; and the
    schedule, night and caps of a run the night shift queued."""
    r = tables.runs
    copied = (
        "kind",
        "project_id",
        "plan_id",
        "step_key",
        "title",
        "plan_revision",
        "dispatched_by",
        "dispatched_via",
        "pinned_worker_id",
        "requested_runtime",
        "mode",
        "approval",
        "timeout_s",
        "max_attempts",
        "repo",
        "branch",
        "repos",
        "model",
        "schedule_id",
        "schedule_night",
        "budget",
    )
    source = select(
        *(r.c[name] for name in copied),
        r.c.requested_runtime.label("runtime"),
        (r.c.attempt + 1).label("attempt"),
        r.c.id.label("parent_run_id"),
    ).where(r.c.id == run_id)
    return insert(r).from_select([*copied, "runtime", "attempt", "parent_run_id"], source).returning(r.c.id)


async def end_held(
    conn: AsyncConnection,
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
    next_id = (await conn.execute(_next_attempt(run_id))).scalar_one()
    await notify_queued(conn, next_id)
    return "lost"


async def release_runs(conn: AsyncConnection, worker_id: int, reason: str) -> int:
    """What the reaper does to the runs of a worker that will never extend a lease again, in the caller's
    transaction: each run it holds is cancelled when its cancel was asked for, fails when it is pinned to this worker
    or on its last attempt, and is otherwise lost, with the next attempt queued for the same step. Queued runs pinned
    to the worker fail, since no other worker may claim them, and runs parked on it are cancelled, since no other
    worker has their session. Returns how many runs were moved."""
    r = tables.runs
    moved = 0
    held = (
        select(
            r.c.id,
            r.c.state,
            r.c.attempt,
            r.c.max_attempts,
            r.c.pinned_worker_id,
            r.c.cancel_requested_at.is_not(None).label("cancel"),
        )
        .where(r.c.worker_id == worker_id, r.c.state.in_(runs.HELD_STATES))
        .order_by(r.c.id)
        .with_for_update()
    )
    for row in (await conn.execute(held)).all():
        await end_held(
            conn,
            row.id,
            row.state,
            attempt=row.attempt,
            max_attempts=row.max_attempts,
            cancel=row.cancel,
            pinned_here=row.pinned_worker_id == worker_id,
            reason=reason,
        )
        moved += 1
    pinned_queued = (
        select(r.c.id, r.c.state)
        .where(r.c.pinned_worker_id == worker_id, r.c.state == "queued")
        .order_by(r.c.id)
        .with_for_update()
    )
    for row in (await conn.execute(pinned_queued)).all():
        error = f"{reason}, and the run was pinned to it"
        await move_run(conn, row.id, row.state, "failed", "reaper", reason=reason, error=error)
        moved += 1
    parked_on = (
        select(r.c.id).where(r.c.worker_id == worker_id, r.c.state == "parked").order_by(r.c.id).with_for_update()
    )
    for run_id in (await conn.execute(parked_on)).scalars().all():
        why = f"{reason}, and only it has the session of the parked run"
        await move_run(conn, run_id, "parked", "cancelled", "reaper", reason=why)
        moved += 1
    return moved


async def end_timed_out(engine: AsyncEngine, batch: int, ended: Counter) -> None:
    """Fail every held run whose agent time is past its timeout, or cancel it when its cancel was asked for, each in a
    transaction of its own; count them in ``ended``."""
    r = tables.runs
    late_held = (r.c.state.in_(runs.HELD_STATES), _past_timeout())
    async with engine.begin() as conn:
        late = (await conn.execute(select(r.c.id).where(*late_held).order_by(r.c.id).limit(batch))).scalars().all()
    for run_id in late:
        async with engine.begin() as conn:
            lock = (
                select(r.c.state, r.c.timeout_s, r.c.cancel_requested_at.is_not(None).label("cancel"))
                .where(r.c.id == run_id, *late_held)
                .with_for_update(skip_locked=True)
            )
            row = (await conn.execute(lock)).one_or_none()
            if row is None:  # it ended meanwhile, or another pass took it
                continue
            state, timeout_s, cancel = row.state, row.timeout_s, row.cancel
            reason = f"it ran past its timeout of {timeout_s // 60} minutes"
            new = "cancelled" if cancel else "failed"
            await move_run(conn, run_id, state, new, "reaper", reason=reason, error=None if cancel else reason)
            ended[new] += 1


async def park_waiting(engine: AsyncEngine, batch: int, wait: timedelta, ended: Counter) -> None:
    """Park every run that has waited ``wait`` for its owner's answer, or cancel it when its cancel was asked for,
    each in a transaction of its own; count them in ``ended``."""
    r = tables.runs
    waited_long = (r.c.state == "waiting", r.c.waiting_since < func.now() - wait)
    async with engine.begin() as conn:
        query = select(r.c.id).where(*waited_long).order_by(r.c.waiting_since).limit(batch)
        waited = (await conn.execute(query)).scalars().all()
    hours = _hours(wait)
    for run_id in waited:
        async with engine.begin() as conn:
            lock = (
                select(r.c.cancel_requested_at.is_not(None).label("cancel"))
                .where(r.c.id == run_id, *waited_long)
                .with_for_update(skip_locked=True)
            )
            row = (await conn.execute(lock)).one_or_none()
            if row is None:  # answered, ended or taken by another pass meanwhile
                continue
            reason = f"nobody answered its decision within {hours}"
            new = "cancelled" if row.cancel else "parked"
            await move_run(conn, run_id, "waiting", new, "reaper", reason=reason)
            ended[new] += 1


async def expire_parked(engine: AsyncEngine, batch: int, parked_for: timedelta, ended: Counter) -> None:
    """Cancel every run parked for ``parked_for``, its open decisions expired, each in a transaction of its own;
    count them in ``ended``."""
    r = tables.runs
    parked_long = (r.c.state == "parked", r.c.parked_at < func.now() - parked_for)
    async with engine.begin() as conn:
        query = select(r.c.id).where(*parked_long).order_by(r.c.parked_at).limit(batch)
        parked = (await conn.execute(query)).scalars().all()
    for run_id in parked:
        async with engine.begin() as conn:
            lock = select(r.c.id).where(r.c.id == run_id, *parked_long).with_for_update(skip_locked=True)
            if (await conn.execute(lock)).one_or_none() is None:
                continue
            reason = f"it stayed parked for {_hours(parked_for)} without an answer"
            await move_run(conn, run_id, "parked", "cancelled", "reaper", reason=reason, decisions="expired")
            ended["cancelled"] += 1


def _hours(span: timedelta) -> str:
    """``span`` in words: whole hours up to two days, then whole days, else seconds."""
    seconds = int(span.total_seconds())
    if seconds % 3600 == 0 and seconds <= 2 * 86400:
        return f"{seconds // 3600} hour{'s' if seconds != 3600 else ''}"
    if seconds % 86400 == 0:
        return f"{seconds // 86400} days"
    return f"{seconds} seconds"


async def recover_runs(
    engine: AsyncEngine,
    batch: int = RECOVER_BATCH,
    *,
    decision_wait: timedelta = DECISION_WAIT,
    parked_for: timedelta = PARKED_FOR,
    sealer: Sealer | None = None,
    github_app: GitHubApp | None = None,
) -> dict:
    """One pass of the reaper: every held run past its timeout ends as ``end_timed_out`` says, then every held run
    whose lease ran out ends as ``end_held`` says, a run that waited ``decision_wait`` for an answer is parked and one
    parked for ``parked_for`` cancelled, each in a transaction of its own, and a queued run pinned to a revoked worker
    fails. Last, the GitHub tokens of the leases given back, by these runs or earlier, are revoked at GitHub with
    ``github_app`` (``credentials.revoke_tokens``; nothing without the App or ``sealer``). Returns how many runs
    ended in each state, and how many were parked."""
    r, w = tables.runs, tables.workers
    ended: Counter[str] = Counter()
    await end_timed_out(engine, batch, ended)
    lapsed = (r.c.state.in_(runs.HELD_STATES), r.c.lease_expires_at < func.now())
    async with engine.begin() as conn:
        query = select(r.c.id).where(*lapsed).order_by(r.c.lease_expires_at).limit(batch)
        expired = (await conn.execute(query)).scalars().all()
    for run_id in expired:
        async with engine.begin() as conn:
            lock = (
                select(
                    r.c.state,
                    r.c.attempt,
                    r.c.max_attempts,
                    r.c.cancel_requested_at.is_not(None).label("cancel"),
                    w.c.name.label("worker"),
                )
                .select_from(r.outerjoin(w, w.c.id == r.c.worker_id))
                .where(r.c.id == run_id, *lapsed)
                .with_for_update(of=r, skip_locked=True)
            )
            row = (await conn.execute(lock)).one_or_none()
            if row is None:  # a heartbeat extended it, or another pass took it
                continue
            reason = f"its worker {row.worker} stopped extending the lease"
            ended[
                await end_held(
                    conn,
                    run_id,
                    row.state,
                    attempt=row.attempt,
                    max_attempts=row.max_attempts,
                    cancel=row.cancel,
                    pinned_here=False,
                    reason=reason,
                )
            ] += 1
    await park_waiting(engine, batch, decision_wait, ended)
    await expire_parked(engine, batch, parked_for, ended)
    async with engine.begin() as conn:
        unclaimable = (
            select(r.c.id, w.c.name)
            .join_from(r, w, w.c.id == r.c.pinned_worker_id)
            .where(r.c.state == "queued", w.c.revoked_at.is_not(None))
            .order_by(r.c.id)
            .limit(batch)
            .with_for_update(of=r, skip_locked=True)
        )
        for run_id, worker in (await conn.execute(unclaimable)).all():
            reason = f"its pinned worker {worker} was revoked"
            error = f"{reason}, so no worker may claim it"
            await move_run(conn, run_id, "queued", "failed", "reaper", reason=reason, error=error)
            ended["failed"] += 1
    report = {state: ended.get(state, 0) for state in ("lost", "failed", "cancelled", "parked")}
    if expired or ended:
        log.warning("runs recovered", extra=report)
    from evo_agents.hub.server import credentials

    await credentials.revoke_tokens(engine, sealer, github_app)
    return report


async def prune_run_events(engine: AsyncEngine, days: int) -> dict:
    """Delete the events of runs that ended more than ``days`` days ago, and drop the sealed values of the GitHub
    tokens leased to runs that are past their end (``credentials.drop_expired``)."""
    from evo_agents.hub.server import credentials

    r, e = tables.runs, tables.run_events
    prune = delete(e).where(
        e.c.run_id == r.c.id,
        r.c.finished_at.is_not(None),
        r.c.finished_at < func.now() - timedelta(days=days),
    )
    async with engine.begin() as conn:
        deleted = (await conn.execute(prune)).rowcount
        dropped = await credentials.drop_expired(conn)
    log.info("run events pruned", extra={"deleted": deleted, "days": days, "tokens_dropped": dropped})
    return {"deleted": deleted, "days": days, "tokens_dropped": dropped}
