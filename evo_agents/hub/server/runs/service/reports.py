"""What a worker reports: its heartbeat (POST /v1/worker/heartbeat), which records the machine, extends the leases
of the runs it holds and answers what to do with each, and the move of a run it holds (POST
/v1/worker/runs/{id}/state), checked against the transition table with the worker as actor."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import and_, case, exists, func, or_, select, update

from evo_agents.hub import runs, tables
from evo_agents.hub.server.run_state import evidence, move_run, run_step, settle
from evo_agents.hub.server.runs.models import NOT_HELD, HeartbeatAnswer, HeartbeatRequest, Run, RunControl, StateReport
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.runs.service.views import beating, run_view
from evo_agents.hub.server.security import Principal

log = logging.getLogger("evo_agents.hub.server.runs")  # the package's one logger, as before it was split


def _steady_since():
    """What a heartbeat sets workers.steady_since to: now for the worker's first heartbeat, or one that came more
    than STEADY_GAP after the one before (which the SET reads as the row was), else what it was."""
    w = tables.workers
    late = or_(w.c.steady_since.is_(None), w.c.last_heartbeat_at.is_(None), ~beating(w))
    return case((late, func.now()), else_=w.c.steady_since)


def _extend(worker_id: int, run_ids: list[int], lease: timedelta):
    """Extend the leases of the runs of ``run_ids`` the worker holds and settle their agent time; per run, its id,
    state, whether a cancel was asked, the new lease, whether a takeover and a handback were asked, and whether the
    owner ended the chat of an author run."""
    r = tables.runs
    return (
        update(r)
        .values(lease_expires_at=func.now() + lease, **settle())
        .where(r.c.worker_id == worker_id, r.c.id.in_(run_ids), r.c.state.in_(runs.HELD_STATES))
        .returning(
            r.c.id,
            r.c.state,
            r.c.cancel_requested_at.is_not(None),
            r.c.lease_expires_at,
            r.c.takeover_requested_at.is_not(None),
            r.c.handback_requested_at.is_not(None),
            r.c.finish_requested_at.is_not(None),
        )
    )


def _parked_here(worker_id: int, run_ids: list[int]):
    """Runs of ``run_ids`` the worker should let go of without cancelling: parked, or done because a new run resumes
    them, which needs their session and worktrees on this worker."""
    r, resumed_by = tables.runs, tables.runs.alias("n")
    resumed = exists().where(resumed_by.c.resume_of_run_id == r.c.id)
    return select(r.c.id, r.c.state).where(
        r.c.worker_id == worker_id,
        r.c.id.in_(run_ids),
        or_(r.c.state == "parked", and_(r.c.state == "done", resumed)),
    )


def _per_run(run_id, *conditions):
    """How many rows match ``conditions`` for each run (``run_id``, a run_id column), runs without one left out."""
    return select(run_id, func.count().label("rows")).where(*conditions).group_by(run_id)


async def record_heartbeat(
    engine, user: Principal, body: HeartbeatRequest, lease: timedelta, terminals
) -> HeartbeatAnswer:
    """Record the machine, extend the leases of the runs it holds by ``lease``, and say what it should do with them;
    ``terminals`` says which runs a browser waits to drive (``terminal.Terminals.waiting``)."""
    reported = list(dict.fromkeys(body.runs))
    runtimes, checkouts = body.stored()
    async with engine.begin() as conn:
        worker_id, _, _, _, _, _, drained_at, *_ = await worker_of(conn, user)
        w = tables.workers
        await conn.execute(
            update(w)
            .values(
                last_heartbeat_at=func.now(),
                steady_since=_steady_since(),
                runtimes=runtimes,
                checkouts=checkouts,
                free_slots=func.least(body.free_slots, w.c.slots),
                agent_version=func.coalesce(body.agent_version, w.c.agent_version),
                run_kinds=None if body.run_kinds is None else [k for k in body.run_kinds if k in runs.RUN_KINDS],
            )
            .where(w.c.id == worker_id)
        )
        extended = {row[0]: row[1:] for row in await conn.execute(_extend(worker_id, reported, lease))}
        others = [run_id for run_id in reported if run_id not in extended]
        parked = {}
        if others:
            parked = {row.id: row.state for row in await conn.execute(_parked_here(worker_id, others))}
        known = [*extended, *parked]
        inbox, decisions = tables.run_inbox, tables.decisions
        waiting, open_decisions = {}, {}
        if extended:
            undelivered = _per_run(inbox.c.run_id, inbox.c.run_id.in_(list(extended)), inbox.c.delivered_at.is_(None))
            waiting = {row.run_id: row.rows for row in await conn.execute(undelivered)}
        if known:
            still_open = _per_run(decisions.c.run_id, decisions.c.run_id.in_(known), decisions.c.state == "open")
            open_decisions = {row.run_id: row.rows for row in await conn.execute(still_open)}
    controls = []
    for run_id in reported:
        if run_id in extended:
            state, cancel, lease, takeover, handback, finish = extended[run_id]
            controls.append(
                RunControl(
                    id=run_id,
                    held=True,
                    state=state,
                    lease_expires_at=lease,
                    cancel=cancel,
                    takeover=takeover,
                    handback=handback,
                    finish=finish,
                    terminal_open=terminals.waiting(run_id),
                    inbox=waiting.get(run_id, 0),
                    decisions=open_decisions.get(run_id, 0),
                )
            )
        elif run_id in parked:  # let go of it without cancelling: its session goes on, or will, on this worker
            controls.append(
                RunControl(
                    id=run_id,
                    held=False,
                    state=parked[run_id],
                    lease_expires_at=None,
                    cancel=False,
                    park=True,
                    decisions=open_decisions.get(run_id, 0),
                )
            )
        else:
            controls.append(RunControl(id=run_id, held=False, state=None, lease_expires_at=None, cancel=True))
    return HeartbeatAnswer(drain=drained_at is not None, runs=controls)


SAME_COLUMNS = ("session_id", "commit_sha", "diffstat", "verify", "usage")  # what a report without a move may set


def _waits_for(run_id: int):
    """Whether run ``run_id`` has something to wait for: a decision still open, or an answer the worker has not taken
    for the agent yet."""
    decisions, inbox = tables.decisions, tables.run_inbox
    open_decision = exists().where(decisions.c.run_id == run_id, decisions.c.state == "open")
    answer = exists().where(inbox.c.run_id == run_id, inbox.c.decision_id.is_not(None), inbox.c.delivered_at.is_(None))
    return select(or_(open_decision, answer))


def _reported_columns(body: StateReport) -> dict:
    columns = {
        "session_id": body.session_id,
        "commit_sha": body.commit_sha,
        "diffstat": body.diffstat.model_dump() if body.diffstat else None,
        "verify": [item.model_dump(exclude_none=True) for item in body.verify] if body.verify is not None else None,
        "usage": body.usage,
    }
    return {name: value for name, value in columns.items() if value is not None}


async def apply_state_report(app_state, user: Principal, run_id: int, body: StateReport) -> Run:
    """Move a run this worker holds, as the transition table lets a worker. Once a move out of the held states
    commits, the GitHub tokens of the leases it gave back are revoked, before the answer."""
    async with app_state.engine.begin() as conn:
        worker_id, _, name, *_ = await worker_of(conn, user)
        row = await _reported_run(conn, run_id, worker_id)
        state, _, _, cancel_requested_at, kind, _ = row
        columns = _reported_columns(body)
        if state == body.state:  # a resend, or news without a move: kept, and nothing moves
            news = {name: value for name, value in columns.items() if name in SAME_COLUMNS}
            if news:  # a column the report leaves out keeps its value
                r = tables.runs
                await conn.execute(update(r).values(**news).where(r.c.id == run_id))
            return await run_view(conn, run_id)
        await _check_move(conn, run_id, row, body)
        error = await _moved_columns(conn, run_id, name, body, columns)
        reason = _move_reason(body, name, error, cancel_requested_at)
        await move_run(
            conn,
            run_id,
            state,
            body.state,
            "worker",
            reason=reason,
            error=error,
            columns=columns,
            token_id=user.token_id,
        )
        if body.state == "waiting" and kind == "author":
            from evo_agents.hub.server import author_chat  # it reads runs through this package

            await author_chat.notify_waiting(conn, run_id)
        view = await run_view(conn, run_id)
    log.info("run moved", extra={"run_id": run_id, "from": state, "to": body.state, "worker_id": worker_id})
    if body.state not in runs.HELD_STATES:  # the move gave back the run's leases: revoke its GitHub tokens now
        from evo_agents.hub.server import credentials

        await credentials.revoke_tokens(
            app_state.engine, app_state.sealer, credentials.revoker(app_state), run_id=run_id
        )
    return view


async def _reported_run(conn, run_id: int, worker_id: int):
    """The row of run ``run_id``, locked, when worker ``worker_id`` claimed it: state, approval, worker_id,
    cancel_requested_at, kind and finish_requested_at. 404 for a run the worker never claimed."""
    r = tables.runs
    reported_run = (
        select(r.c.state, r.c.approval, r.c.worker_id, r.c.cancel_requested_at, r.c.kind, r.c.finish_requested_at)
        .where(r.c.id == run_id)
        .with_for_update()
    )
    row = (await conn.execute(reported_run)).one_or_none()
    if row is None or row.worker_id != worker_id:
        raise HTTPException(404, NOT_HELD.format(id=run_id))
    return row


async def _check_move(conn, run_id: int, row, body: StateReport) -> None:
    """Refuse a move the worker may not make from where run ``run_id`` (``row``, as ``_reported_run`` reads it) is:
    404 for a run it no longer holds, 409 for a move the transition table, the run's approval or its kind does not
    allow, and for a wait with nothing to wait for."""
    state, approval, _, _, kind, finish_requested_at = row
    if body.from_state is not None and body.from_state != state:
        raise HTTPException(409, f"run {run_id} is {state}, not {body.from_state}")
    if state not in runs.HELD_STATES:
        raise HTTPException(404, NOT_HELD.format(id=run_id))
    try:
        runs.check_transition(state, body.state, "worker")
    except runs.TransitionRefused as exc:
        raise HTTPException(409, f"run {run_id}: {exc}") from None
    _check_verdict(run_id, kind, approval, body)
    if body.state == "waiting" and kind == "author" and finish_requested_at is not None:
        raise HTTPException(
            409,
            f"run {run_id}: its owner ended the chat, so the author run waits for no reply: end it done",
        )
    if body.state == "waiting" and kind != "author" and not (await conn.execute(_waits_for(run_id))).scalar_one():
        raise HTTPException(
            409,
            f"run {run_id} has no open decision and no answer waiting for the agent: a run waits only for the "
            "answer to a decision of its own, and the time it waits does not count toward its timeout",
        )


async def _moved_columns(conn, run_id: int, name: str, body: StateReport, columns: dict) -> str | None:
    """The error the move of run ``run_id`` records (one for a failure the worker ``name`` gave none for), and what
    else it sets, added to ``columns``: the cause of a failure, and the evidence of a run that ends done or in
    review."""
    error = body.error
    if body.state == "failed" and error is None:
        error = f"worker {name} reported that the run failed"
    if body.state == "failed" and body.failure_cause is not None:
        columns["failure_cause"] = body.failure_cause
    if body.state in ("done", "review"):
        found = await run_step(conn, run_id)
        found = replace(
            found,
            commit_sha=columns.get("commit_sha", found.commit_sha),
            diffstat=columns.get("diffstat", found.diffstat),
            verify=columns.get("verify", found.verify),
        )
        columns["evidence"] = evidence(found, body.summary)
    return error


def _move_reason(body: StateReport, name: str, error: str | None, cancel_requested_at) -> str:
    """Why the run moved, as its state event says: the owner's cancel or the worker ``name`` stopping it, the
    error, or the state the worker reported."""
    if body.state == "cancelled":
        return "its owner asked to cancel it" if cancel_requested_at else f"worker {name} stopped it"
    if error:
        return error
    return f"worker {name} reported {body.state}"


def _check_verdict(run_id: int, kind: str, approval: str, body: StateReport) -> None:
    """409 for a verdict the run's approval, or its kind, does not allow. A plan run ends done without verify
    results, since each of its steps was verified when it was reported, and never waits in review."""
    if kind in ("plan", "review", "judge", "author"):
        if body.state == "review":
            article = "an" if kind[:1] in "aeiou" else "a"
            raise HTTPException(409, f"run {run_id} is {article} {kind} run: report done or failed, not review")
        if body.state == "done" and any(item.exit_code != 0 for item in body.verify or []):
            raise HTTPException(409, f"run {run_id} is done only when every verify command it reports exited 0")
        return
    if body.state == "done":
        if approval != "auto":
            raise HTTPException(409, f"run {run_id} waits for its owner's approval: report review, not done")
        if not body.verify or any(item.exit_code != 0 for item in body.verify):
            raise HTTPException(
                409,
                f"run {run_id} is done only when every verify command exited 0; report the results, or failed",
            )
    if body.state == "review" and approval != "review":
        raise HTTPException(409, f"run {run_id} has approval auto: report done or failed, not review")
