"""The owner's controls of a run: cancel, takeover, handback, approve and rerun, each for the member who dispatched the
run alone (403 for another member, 404 without a grant on the project), and the refusal of a token that would steer a
run on a worker set to take runs dispatched from the web only (``web_only_steering``)."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.server import audit
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.run_state import move_run
from evo_agents.hub.server.runs.models import Run
from evo_agents.hub.server.runs.service.audits import audit_run, run_target
from evo_agents.hub.server.runs.service.dispatch import (
    check_dispatcher,
    no_plan_run,
    pinnable,
    queue_run,
    refuse_curator_plan,
)
from evo_agents.hub.server.runs.service.views import lock_plan, plan_activity, run_view
from evo_agents.hub.server.security import WEB, Principal

log = logging.getLogger("evo_agents.hub.server.runs")  # the package's one logger, as before it was split


async def owned_run(conn: AsyncConnection, user: Principal, project: str, run_id: int, action: str):
    """(access, row) for a run of ``project`` that ``user`` dispatched, its row locked: dispatched_by, login, state,
    plan_id, step_key, cancel_requested_at, requested_runtime, mode, approval, timeout_s, pinned_worker_id, kind and
    model. 404 for a run ``user`` cannot see, 403 for another member's."""
    access = await project_access(conn, user, project)
    plan_routes._reader(access)
    r, u = tables.runs, tables.users
    query = (
        select(
            r.c.dispatched_by,
            u.c.login,
            r.c.state,
            r.c.plan_id,
            r.c.step_key,
            r.c.cancel_requested_at,
            r.c.requested_runtime,
            r.c.mode,
            r.c.approval,
            r.c.timeout_s,
            r.c.pinned_worker_id,
            r.c.kind,
            r.c.model,
        )
        .join_from(r, u, u.c.id == r.c.dispatched_by)
        .where(r.c.id == run_id, r.c.project_id == access.project_id)
        .with_for_update(of=r)
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        raise HTTPException(404, f"project {project} has no run {run_id}: see GET /v1/projects/{project}/runs")
    if row[0] != user.user_id:
        raise HTTPException(403, f"only {row[1]}, who dispatched run {run_id}, may {action} it")
    return access, row


def _web_only_worker(run_id: int):
    """The worker set to take runs dispatched from the web only that run ``run_id`` is on, or may go to, with how:
    the one holding it (or that parked it), the one it is pinned to, or, for a run queued without a pin and
    dispatched from the web, one of its owner's workers set so, which may claim it; in that order, then by name."""
    r, w = tables.runs, tables.workers
    held, pinned = w.c.id == r.c.worker_id, w.c.id == r.c.pinned_worker_id
    claimable = and_(
        r.c.state == "queued",
        r.c.pinned_worker_id.is_(None),
        r.c.dispatched_via == "web",
        w.c.owner_id == r.c.dispatched_by,
    )
    relation = case(
        (and_(held, r.c.state == "parked"), "was parked on"),
        (held, "is held by"),
        (pinned, "is pinned to"),
        else_="may go to",
    )
    return (
        select(w.c.name, relation.label("relation"))
        .join_from(r, w, and_(w.c.dispatch_from == "web", w.c.revoked_at.is_(None), or_(held, pinned, claimable)))
        .where(r.c.id == run_id)
        .order_by(held.desc(), pinned.desc(), w.c.name)
        .limit(1)
    )


async def web_only_steering(
    conn: AsyncConnection, user: Principal, run_id: int, doing: str, instead: str, undone: str
) -> None:
    """403 when ``user`` is a token, not a web session, and run ``run_id`` is on, or may go to, a worker whose owner
    set it to take runs dispatched from the web only (``_web_only_worker``): a token that cannot hand such a worker
    work cannot steer the work it has either, by a message to its agent or the answer to a decision, which can resume
    a parked run on it. The refusal says ``doing`` was refused, what to do ``instead``, and that nothing was
    ``undone``."""
    if user.kind == WEB:
        return
    row = (await conn.execute(_web_only_worker(run_id))).one_or_none()
    if row is not None:
        name, relation = row
        raise HTTPException(
            403,
            f"run {run_id} {relation} worker {name}, which takes only runs dispatched from a web session, as its owner "
            f"set it, so a token cannot {doing}: {instead}; nothing was {undone}",
        )


async def cancel_run(engine, user: Principal, project: str, run_id: int) -> Run:
    """Cancel a queued run or one in review at once; ask the worker holding a held run to stop it."""
    async with engine.begin() as conn:
        access, row = await owned_run(conn, user, project, run_id, "cancel")
        state, plan_id, key, cancel_requested_at = row[2], row[3], row[4], row[5]
        if state in runs.TERMINAL_STATES:
            raise HTTPException(409, f"run {run_id} is {state}, which is final")
        if state in runs.HELD_STATES:
            if cancel_requested_at is not None:  # asked already: nothing changes, nothing is audited
                return await run_view(conn, run_id)
            r = tables.runs
            await conn.execute(update(r).values(cancel_requested_at=func.now()).where(r.c.id == run_id))
        else:
            reason = f"{user.login} cancelled it"
            await move_run(conn, run_id, state, "cancelled", "owner", reason=reason, token_id=user.token_id)
        await audit_run(conn, user, access, audit.RUN_CANCEL, run_target(project, plan_id, key, run_id))
        view = await run_view(conn, run_id)
    log.info("run cancelled", extra={"run_id": run_id, "state": view.state, "login": user.login})
    return view


async def ask_once(conn: AsyncConnection, run_id: int, asked_at) -> bool:
    """Set ``asked_at``, a column of runs, to now unless it is set already: True when this set it."""
    r = tables.runs
    ask = update(r).values({asked_at: func.now()}).where(r.c.id == run_id, asked_at.is_(None)).returning(r.c.id)
    return (await conn.execute(ask)).one_or_none() is not None


async def ask_takeover(conn: AsyncConnection, run_id: int) -> bool:
    """Ask the worker holding run ``run_id`` for a takeover; False when one was asked already and is still open."""
    return await ask_once(conn, run_id, tables.runs.c.takeover_requested_at)


async def ask_handback(conn: AsyncConnection, run_id: int) -> bool:
    """Ask the worker holding run ``run_id`` for a handback; False when one was asked already and is still open."""
    return await ask_once(conn, run_id, tables.runs.c.handback_requested_at)


async def ask_control(engine, user: Principal, project: str, run_id: int, action: str) -> Run:
    """Ask the worker holding run ``run_id`` for a takeover or a handback, which its next heartbeat says."""
    takeover = action == audit.RUN_TAKEOVER
    allowed, verb = (runs.TAKEOVER_STATES, "take over") if takeover else (runs.HANDBACK_STATES, "hand back")
    async with engine.begin() as conn:
        access, row = await owned_run(conn, user, project, run_id, verb)
        state, plan_id, key = row[2], row[3], row[4]
        if state not in allowed:
            if takeover:
                why = "a person drives it already" if state == "interactive" else "no agent of it runs now"
                needs = "leased or running"
            else:
                why = "it runs headless" if state in runs.TAKEOVER_STATES else "no agent of it runs now"
                needs = "interactive"
            raise HTTPException(409, f"run {run_id} is {state}, so {why}: one may {verb} a run that is {needs}")
        asked = await (ask_takeover if takeover else ask_handback)(conn, run_id)
        if asked:  # an ask repeated while open changes nothing and is not audited again
            await audit_run(conn, user, access, action, run_target(project, plan_id, key, run_id))
        view = await run_view(conn, run_id)
    log.info("run control asked", extra={"action": action, "run_id": run_id, "state": view.state, "login": user.login})
    return view


async def approve_run(engine, user: Principal, project: str, run_id: int) -> Run:
    """Approve a run in review: the run is done, and so is its step."""
    async with engine.begin() as conn:
        access, row = await owned_run(conn, user, project, run_id, "approve")
        check_dispatcher(access)
        state, plan_id, key = row[2], row[3], row[4]
        if state != "review":
            raise HTTPException(409, f"run {run_id} is {state}; only a run in review is approved")
        await move_run(conn, run_id, state, "done", "owner", reason=f"{user.login} approved it", token_id=user.token_id)
        await audit_run(conn, user, access, audit.RUN_APPROVE, run_target(project, plan_id, key, run_id))
        view = await run_view(conn, run_id)
    log.info("run approved", extra={"run_id": run_id, "login": user.login})
    return view


async def rerun_run(engine, user: Principal, project: str, run_id: int) -> Run:
    """Queue the step of a run that ended again, with the same runtime, model, mode, approval, timeout and worker, at
    the plan's current revision."""
    async with engine.begin() as conn:
        access, row = await owned_run(conn, user, project, run_id, "rerun")
        check_dispatcher(access)
        _, _, state, plan_id, key, _, runtime, mode, approval, timeout_s, pinned, kind, model = row
        if kind == "plan":
            raise HTTPException(
                409, f"run {run_id} is a plan run: dispatch the plan again with POST /v1/projects/{project}/plan-runs"
            )
        if kind in runs.CURATOR_KINDS:
            raise HTTPException(
                409, f"run {run_id} is a {kind} run, which only the night shift of the project's charter queues"
            )
        if kind == "author":
            raise HTTPException(
                409, f"run {run_id} is an author run: dispatch a new one with POST /v1/projects/{project}/author-runs"
            )
        if state not in runs.TERMINAL_STATES:
            raise HTTPException(409, f"run {run_id} is still {state}: a run is rerun once it has ended")
        held = await plan_routes._visible(conn, access, plan_id, None)
        await refuse_curator_plan(conn, access, plan_id)
        worker = None if pinned is None else await pinnable(conn, user, access, pinned)
        await lock_plan(conn, access.project_id, plan_id)
        activity = await plan_activity(conn, access.project_id, plan_id)
        no_plan_run(activity, plan_id)
        new_id = await queue_run(
            conn,
            access,
            user,
            held,
            key,
            activity.steps,
            runtime=runtime,
            model=model,
            mode=mode,
            approval=approval,
            timeout_s=timeout_s,
            pinned=worker,
            parent=run_id,
        )
        target = f"{run_target(project, plan_id, key, new_id)} rerun of run:{run_id}"
        await audit_run(conn, user, access, audit.RUN_RERUN, target)
        view = await run_view(conn, new_id)
    log.info("run rerun", extra={"run_id": new_id, "rerun_of": run_id, "login": user.login})
    return view
