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
those states; a heartbeat settles it the same way (``SETTLE``). A move to ``waiting`` sets ``waiting_since`` (any other
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

import psycopg
from fastapi import HTTPException
from psycopg import sql
from psycopg.types.json import Jsonb

from evo_agents.hub import runs
from evo_agents.hub.access import has_role
from evo_agents.hub.db import legacy
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

# Agent time, in an UPDATE of runs (whose right-hand sides read the row as it was) or a query of it: when the count
# last stopped, and the whole seconds since then while the run is in one of runs.CLOCK_STATES (%(clock)s).
CLOCK_FROM = "coalesce(counted_at, started_at, leased_at, now())"
ELAPSED = (
    f"CASE WHEN state = ANY(%(clock)s) THEN greatest(0, floor(extract(epoch FROM now() - {CLOCK_FROM})))::integer "
    "ELSE 0 END"
)
# What a heartbeat sets to settle a run's agent time without moving it; the fraction of a second left goes on counting.
SETTLE = (
    f"run_seconds = run_seconds + {ELAPSED}, "
    f"counted_at = CASE WHEN state = ANY(%(clock)s) THEN {CLOCK_FROM} + make_interval(secs => {ELAPSED}) END"
)
PAST_TIMEOUT = f"run_seconds + {ELAPSED} > timeout_s"

MOVE = f"""
UPDATE runs SET state = %(to)s, error = coalesce(%(error)s, error), event_seq = event_seq + 1,
       leased_at = CASE WHEN %(to)s = 'leased' THEN now() ELSE leased_at END,
       started_at = CASE WHEN %(to)s IN ('running', 'interactive') THEN coalesce(started_at, now())
                         ELSE started_at END,
       lease_expires_at = CASE WHEN %(to)s = ANY(%(held)s) THEN coalesce(lease_expires_at, now() + %(lease)s)
                               ELSE NULL END,
       finished_at = CASE WHEN %(to)s = ANY(%(terminal)s) THEN now() ELSE finished_at END,
       run_seconds = run_seconds + {ELAPSED},
       counted_at = CASE WHEN %(to)s <> ALL(%(clock)s) THEN NULL
                         WHEN state = ANY(%(clock)s) THEN {CLOCK_FROM} + make_interval(secs => {ELAPSED})
                         ELSE now() END,
       waiting_since = CASE WHEN %(to)s = 'waiting' THEN now() END,
       parked_at = CASE WHEN %(to)s = 'parked' THEN now() ELSE parked_at END,
       takeover_requested_at = CASE WHEN %(to)s = ANY(%(takeover)s) THEN takeover_requested_at END,
       handback_requested_at = CASE WHEN %(to)s = ANY(%(handback)s) THEN handback_requested_at END{{extra}}
 WHERE id = %(id)s AND state = %(from)s
RETURNING event_seq
"""
END_DECISIONS = "UPDATE decisions SET state = %s WHERE run_id = %s AND state = 'open'"
# The next attempt keeps the dispatch's credential, so a worker that takes runs dispatched from the web only takes the
# retry of one too, and never the retry of a run dispatched with a token.
NEXT_ATTEMPT = """
INSERT INTO runs (kind, project_id, plan_id, step_key, title, plan_revision, dispatched_by, dispatched_via,
                  pinned_worker_id, requested_runtime, runtime, mode, approval, timeout_s, attempt, max_attempts,
                  parent_run_id, repo, branch, repos, model)
SELECT kind, project_id, plan_id, step_key, title, plan_revision, dispatched_by, dispatched_via, pinned_worker_id,
       requested_runtime, requested_runtime, mode, approval, timeout_s, attempt + 1, max_attempts, id, repo, branch,
       repos, model
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
PARKED_ON = "SELECT id FROM runs WHERE worker_id = %s AND state = 'parked' ORDER BY id FOR UPDATE"
EXPIRED = "SELECT id FROM runs WHERE state = ANY(%s) AND lease_expires_at < now() ORDER BY lease_expires_at LIMIT %s"
TIMED_OUT = f"SELECT id FROM runs WHERE state = ANY(%(held)s) AND {PAST_TIMEOUT} ORDER BY id LIMIT %(batch)s"
LOCK_TIMED_OUT = f"""
SELECT state, timeout_s, cancel_requested_at IS NOT NULL
  FROM runs
 WHERE id = %(id)s AND state = ANY(%(held)s) AND {PAST_TIMEOUT}
   FOR UPDATE SKIP LOCKED
"""
WAITED_TOO_LONG = """
SELECT id FROM runs WHERE state = 'waiting' AND waiting_since < now() - %s ORDER BY waiting_since LIMIT %s
"""
LOCK_WAITED = """
SELECT cancel_requested_at IS NOT NULL
  FROM runs
 WHERE id = %s AND state = 'waiting' AND waiting_since < now() - %s
   FOR UPDATE SKIP LOCKED
"""
PARKED_TOO_LONG = "SELECT id FROM runs WHERE state = 'parked' AND parked_at < now() - %s ORDER BY parked_at LIMIT %s"
LOCK_PARKED = "SELECT 1 FROM runs WHERE id = %s AND state = 'parked' AND parked_at < now() - %s FOR UPDATE SKIP LOCKED"
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
       r.repo, r.branch, r.commit_sha, r.diffstat, r.verify, r.evidence, r.error, r.kind, r.repos, r.project_id
  FROM runs r JOIN projects p ON p.id = r.project_id JOIN users u ON u.id = r.dispatched_by
  LEFT JOIN workers w ON w.id = r.worker_id
 WHERE r.id = %s
"""
PLAN_BODY = "SELECT body FROM plans WHERE project_id = %s AND plan_id = %s"
# The steps a plan run reported, its earlier attempts and the parked runs it went on from included, in no order.
REPORTED_STEPS = """
WITH RECURSIVE chain (id, parent_run_id, resume_of_run_id) AS (
    SELECT id, parent_run_id, resume_of_run_id FROM runs WHERE id = %s
  UNION
    SELECT r.id, r.parent_run_id, r.resume_of_run_id
      FROM runs r JOIN chain c ON r.id = c.parent_run_id OR r.id = c.resume_of_run_id
)
SELECT DISTINCT e.body -> 'step_report' ->> 'step'
  FROM run_events e JOIN chain c ON c.id = e.run_id
 WHERE e.kind = 'system' AND jsonb_typeof(e.body -> 'step_report') = 'object'
   AND jsonb_typeof(e.body -> 'step_report' -> 'step') = 'string'
"""
NEXT_SEQ = "UPDATE runs SET event_seq = event_seq + 1 WHERE id = %s RETURNING event_seq"
INSERT_SYSTEM_EVENT = "INSERT INTO run_events (run_id, seq, kind, body) VALUES (%s, %s, 'system', %s)"


async def notify_queued(conn, run_id: int) -> None:
    """Wake the claims waiting on RUNS_CHANNEL once the caller's transaction commits."""
    await legacy(conn, "SELECT pg_notify(%s, %s)", (RUNS_CHANNEL, str(run_id)))


async def notify_events(conn, run_id: int) -> None:
    """Wake the streams of run ``run_id`` on EVENTS_CHANNEL once the caller's transaction commits; the notifications
    of one transaction with the same run arrive as one."""
    await legacy(conn, "SELECT pg_notify(%s, %s)", (EVENTS_CHANNEL, str(run_id)))


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
        "takeover": list(runs.TAKEOVER_STATES),
        "handback": list(runs.HANDBACK_STATES),
        "clock": list(runs.CLOCK_STATES),
        "lease": lease,
        **{f"set_{name}": _column_value(value) for name, value in columns.items()},
    }
    row = await (await legacy(conn, sql.SQL(MOVE).format(extra=extra), params)).fetchone()
    if row is None:
        raise runs.TransitionRefused(f"run {run_id} is no longer {old}: it moved meanwhile")
    body = {"from": old, "to": new, "actor": actor, "reason": reason}
    await legacy(
        conn,
        "INSERT INTO run_events (run_id, seq, kind, body) VALUES (%s, %s, 'state', %s)",
        (run_id, row[0], Jsonb(body)),
    )
    if new in runs.TERMINAL_STATES and decisions is not None:
        await legacy(conn, END_DECISIONS, (decisions, run_id))
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


async def run_step(conn, run_id: int) -> RunStep:
    row = await (await legacy(conn, RUN_STEP, (run_id,))).fetchone()
    return RunStep(*row)


async def write_event(conn, run_id: int, body: dict) -> int:
    """Write a ``system`` event of the hub's own into the log of run ``run_id``, whose row the caller holds locked,
    and wake its streams; its seq. Like the ``state`` events, it is written past the run's limit of events."""
    seq = (await (await legacy(conn, NEXT_SEQ, (run_id,))).fetchone())[0]
    await legacy(conn, INSERT_SYSTEM_EVENT, (run_id, seq, Jsonb(body)))
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
    conn, found: RunStep, updates_for, *, token_id: int | None = None, strict: bool = False
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
    except psycopg.Error as exc:
        if strict:
            raise
        log.error("run step not written to the plan", extra={**where, "error": f"{type(exc).__name__}: {exc}"})
        return None
    if strict:
        raise refused
    return None


async def record_move(conn, run_id: int, old: str, new: str, *, reason: str, token_id: int | None = None) -> int | None:
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


async def release_plan_steps(conn, found: RunStep, new: str, *, reason: str, token_id: int | None = None) -> int | None:
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


async def reported_steps(conn, run_id: int) -> list[str]:
    """The steps plan run ``run_id`` reported, it or an earlier attempt or parked run it went on from, in the order
    people number them."""
    rows = await (await legacy(conn, REPORTED_STEPS, (run_id,))).fetchall()
    return sorted((row[0] for row in rows), key=_natural)


def _every_step_done(body) -> bool:
    steps = body.get("steps") if isinstance(body, dict) else None
    if not isinstance(steps, list) or not steps:
        return False
    return all(isinstance(step, dict) and step.get("status") == "done" for step in steps)


async def notify_plan_run_end(conn, found: RunStep, old: str, new: str, *, reason: str) -> int | None:
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
    row = await (await legacy(conn, PLAN_BODY, (found.project_id, found.plan_id))).fetchone()
    if row is None or not _every_step_done(row[0]):
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
    next_id = (await (await legacy(conn, NEXT_ATTEMPT, (run_id,))).fetchone())[0]
    await notify_queued(conn, next_id)
    return "lost"


async def release_runs(conn, worker_id: int, reason: str) -> int:
    """What the reaper does to the runs of a worker that will never extend a lease again, in the caller's
    transaction: each run it holds is cancelled when its cancel was asked for, fails when it is pinned to this worker
    or on its last attempt, and is otherwise lost, with the next attempt queued for the same step. Queued runs pinned
    to the worker fail, since no other worker may claim them, and runs parked on it are cancelled, since no other
    worker has their session. Returns how many runs were moved."""
    moved = 0
    for run_id, state, attempt, max_attempts, pinned, cancel in await (
        await legacy(conn, HELD_RUNS, (worker_id, list(runs.HELD_STATES)))
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
    for run_id, state in await (await legacy(conn, PINNED_QUEUED, (worker_id,))).fetchall():
        error = f"{reason}, and the run was pinned to it"
        await move_run(conn, run_id, state, "failed", "reaper", reason=reason, error=error)
        moved += 1
    for (run_id,) in await (await legacy(conn, PARKED_ON, (worker_id,))).fetchall():
        why = f"{reason}, and only it has the session of the parked run"
        await move_run(conn, run_id, "parked", "cancelled", "reaper", reason=why)
        moved += 1
    return moved


async def end_timed_out(engine, batch: int, ended: Counter) -> None:
    """Fail every held run whose agent time is past its timeout, or cancel it when its cancel was asked for, each in a
    transaction of its own; count them in ``ended``."""
    params = {"held": list(runs.HELD_STATES), "clock": list(runs.CLOCK_STATES), "batch": batch}
    async with engine.begin() as conn:
        late = [row[0] for row in await (await legacy(conn, TIMED_OUT, params)).fetchall()]
    for run_id in late:
        async with engine.begin() as conn:
            row = await (await legacy(conn, LOCK_TIMED_OUT, {**params, "id": run_id})).fetchone()
            if row is None:  # it ended meanwhile, or another pass took it
                continue
            state, timeout_s, cancel = row
            reason = f"it ran past its timeout of {timeout_s // 60} minutes"
            new = "cancelled" if cancel else "failed"
            await move_run(conn, run_id, state, new, "reaper", reason=reason, error=None if cancel else reason)
            ended[new] += 1


async def park_waiting(engine, batch: int, wait: timedelta, ended: Counter) -> None:
    """Park every run that has waited ``wait`` for its owner's answer, or cancel it when its cancel was asked for,
    each in a transaction of its own; count them in ``ended``."""
    async with engine.begin() as conn:
        waited = [row[0] for row in await (await legacy(conn, WAITED_TOO_LONG, (wait, batch))).fetchall()]
    hours = _hours(wait)
    for run_id in waited:
        async with engine.begin() as conn:
            row = await (await legacy(conn, LOCK_WAITED, (run_id, wait))).fetchone()
            if row is None:  # answered, ended or taken by another pass meanwhile
                continue
            reason = f"nobody answered its decision within {hours}"
            new = "cancelled" if row[0] else "parked"
            await move_run(conn, run_id, "waiting", new, "reaper", reason=reason)
            ended[new] += 1


async def expire_parked(engine, batch: int, parked_for: timedelta, ended: Counter) -> None:
    """Cancel every run parked for ``parked_for``, its open decisions expired, each in a transaction of its own;
    count them in ``ended``."""
    async with engine.begin() as conn:
        parked = [row[0] for row in await (await legacy(conn, PARKED_TOO_LONG, (parked_for, batch))).fetchall()]
    for run_id in parked:
        async with engine.begin() as conn:
            if (await (await legacy(conn, LOCK_PARKED, (run_id, parked_for))).fetchone()) is None:
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
    engine,
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
    ended: Counter[str] = Counter()
    await end_timed_out(engine, batch, ended)
    async with engine.begin() as conn:
        expired = [row[0] for row in await (await legacy(conn, EXPIRED, (list(runs.HELD_STATES), batch))).fetchall()]
    for run_id in expired:
        async with engine.begin() as conn:
            row = await (await legacy(conn, LOCK_EXPIRED, (run_id, list(runs.HELD_STATES)))).fetchone()
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
    await park_waiting(engine, batch, decision_wait, ended)
    await expire_parked(engine, batch, parked_for, ended)
    async with engine.begin() as conn:
        for run_id, worker in await (await legacy(conn, UNCLAIMABLE, (batch,))).fetchall():
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


async def prune_run_events(engine, days: int) -> dict:
    """Delete the events of runs that ended more than ``days`` days ago, and drop the sealed values of the GitHub
    tokens leased to runs that are past their end (``credentials.drop_expired``)."""
    from evo_agents.hub.server import credentials

    async with engine.begin() as conn:
        deleted = (await legacy(conn, PRUNE_EVENTS, (days,))).rowcount
        dropped = await credentials.drop_expired(conn)
    log.info("run events pruned", extra={"deleted": deleted, "days": days, "tokens_dropped": dropped})
    return {"deleted": deleted, "days": days, "tokens_dropped": dropped}
