"""The owner's controls of a run: cancel, takeover, handback, approve and rerun, each for the member who dispatched the
run alone (403 for another member, 404 without a grant on the project), and the refusal of a token that would steer a
run on a worker set to take runs dispatched from the web only (``web_only_steering``)."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.server import audit, run_access
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.plugin_configs import run_layer_of
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.run_state import move_run
from evo_agents.hub.server.runs.models import AccessChange, Rerun, Run
from evo_agents.hub.server.runs.service import browsers
from evo_agents.hub.server.runs.service.accounts import rerun_picks
from evo_agents.hub.server.runs.service.audits import audit_run, run_target
from evo_agents.hub.server.runs.service.dispatch import (
    check_dispatcher,
    check_seat,
    no_plan_run,
    pinnable,
    placement,
    queue_run,
    refuse_curator_plan,
)
from evo_agents.hub.server.runs.service.profiles import Asked, rerun_choice
from evo_agents.hub.server.runs.service.views import lock_plan, plan_activity, run_view
from evo_agents.hub.server.runs.service.worker_fit import shared_in
from evo_agents.hub.server.security import WEB, Principal

log = logging.getLogger("evo_agents.hub.server.runs")  # the package's one logger, as before it was split
# How a run of each kind that is never rerun runs again.
AGAIN = {"chat": "mention its agent in the chat again", "task": "its plugin hands the task to its agent type again"}


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
    if row.kind == "local":
        raise HTTPException(409, f"run {run_id} is a local session the hub only watches: it cannot {action} it")
    return access, row


def _web_only_worker(run_id: int):
    """The worker set to take runs dispatched from the web only that run ``run_id`` is on, or may go to, with how:
    the one holding it (or that parked it), the one it is pinned to, or, for a run queued without a pin and
    dispatched from the web, one of its owner's workers set so, or one another member shares in its project
    (``shared_in``), which may claim it; in that order, then by name."""
    r, w = tables.runs, tables.workers
    held, pinned = w.c.id == r.c.worker_id, w.c.id == r.c.pinned_worker_id
    claimable = and_(
        r.c.state == "queued",
        r.c.pinned_worker_id.is_(None),
        r.c.dispatched_via == "web",
        or_(w.c.owner_id == r.c.dispatched_by, shared_in(r.c.project_id, r.c.dispatched_by)),
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


async def rerun_run(engine, user: Principal, project: str, run_id: int, body: Rerun | None = None) -> Run:
    """Queue the step of a run that ended again, with the same runtime, model, mode, approval, timeout and worker, at
    the plan's current revision, and the account picked by hand for it unless ``body`` picks another one or none,
    under the agent profile of the run at the profile's version now, checked again (``profiles.rerun_choice``), with
    the run's own layer of plugin config."""
    async with engine.begin() as conn:
        view = await rerun_in(conn, user, project, run_id, body)
    log.info("run rerun", extra={"run_id": view.id, "rerun_of": run_id, "login": user.login})
    return view


async def _rerun_placement(conn: AsyncConnection, run_id: int, body: Rerun | None) -> dict:
    """The selector and tolerations of a rerun of run ``run_id``: those ``body`` names, else the run's own."""
    if body is not None and body.placed():
        return placement(body)
    r = tables.runs
    row = (await conn.execute(select(r.c.selector, r.c.tolerations).where(r.c.id == run_id))).one()
    return {"selector": tuple(row.selector or ()), "tolerations": tuple(row.tolerations or ())}


async def rerun_in(
    conn: AsyncConnection,
    user: Principal,
    project: str,
    run_id: int,
    body: Rerun | None = None,
    *,
    worker_id: int | None = None,
    runtime: str | None = None,
) -> Run:
    """``rerun_run`` in the caller's transaction. ``worker_id`` pins the rerun to that worker of ``user`` instead of
    the run's own pin, and ``runtime`` asks for that runtime instead of the run's, with the profile's model then (the
    triage of a failed run, ``evo_agents.hub.server.triage_actions``, is the one that changes either)."""
    access, row = await owned_run(conn, user, project, run_id, "rerun")
    check_dispatcher(access)
    await check_seat(conn, user, "running a run again")
    _, _, state, plan_id, key, _, asked_runtime, mode, approval, timeout_s, pinned, kind, model = row
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
    if kind in AGAIN:
        raise HTTPException(409, f"run {run_id} is a {kind} run: {AGAIN[kind]}")
    if state not in runs.TERMINAL_STATES:
        raise HTTPException(409, f"run {run_id} is still {state}: a run is rerun once it has ended")
    runtime_now = runtime or asked_runtime
    model = None if runtime else model
    held = await plan_routes._visible(conn, access, plan_id, None)
    await refuse_curator_plan(conn, access, plan_id)
    pin_to = pinned if worker_id is None else worker_id
    worker = None if pin_to is None else await pinnable(conn, user, access, pin_to)
    placed = await _rerun_placement(conn, run_id, body)
    picked = await rerun_picks(conn, user, access, run_id, body, worker, placed["selector"])
    asked = Asked(kind, runtime_now, model, worker, tuple(picked), **placed)
    chosen = await rerun_choice(conn, user, access, run_id, asked)
    await lock_plan(conn, access.project_id, plan_id)
    activity = await plan_activity(conn, access.project_id, plan_id)
    no_plan_run(activity, plan_id)
    browser, identities = await browsers.rerun_browser(conn, user, run_id, body, worker)
    as_owner = await _rerun_as_owner(conn, run_id, body)
    run_access.refuse_onward(user, as_owner)
    new_id = await queue_run(
        conn,
        access,
        user,
        held,
        key,
        activity.steps,
        runtime=runtime_now,
        model=chosen.model,
        mode=mode,
        approval=approval,
        timeout_s=timeout_s,
        pinned=worker,
        parent=run_id,
        picked=tuple(picked),
        chosen=chosen,
        plugin_config=await run_layer_of(conn, run_id),
        browser=browser,
        identities=identities,
        placed=placed,
        as_owner=as_owner,
    )
    accounts = "".join(account.audit_text() for account in picked) + chosen.audit_text()
    target = f"{run_target(project, plan_id, key, new_id)} rerun of run:{run_id}{accounts}"
    await audit_run(conn, user, access, audit.RUN_RERUN, target)
    if as_owner:
        granted = f"{run_target(project, plan_id, key, new_id)} by=rerun of run:{run_id}"
        await run_access.record(conn, user, access.project_id, granted, granted=True)
    return await run_view(conn, new_id)


async def _rerun_as_owner(conn: AsyncConnection, run_id: int, body: Rerun | None) -> bool:
    """Whether the rerun of run ``run_id`` runs as its owner: as ``body`` says, else as the run did."""
    if body is not None and body.as_me is not None:
        return body.as_me
    r = tables.runs
    return bool((await conn.execute(select(r.c.as_owner).where(r.c.id == run_id))).scalar_one())


async def change_access(engine, user: Principal, project: str, run_id: int, body: AccessChange) -> Run:
    """Let a run of the caller's run as them, or stop it (``run_access``): the run's row changes, a stop ends the
    run's token at once, and either is audited; asking for what the run does already changes nothing and is not. Only
    the run's owner (403 for another member), never with a run's token (``run_access.refuse_onward``, as the
    middleware's group run access refuses the route already); to let it, a run of a kind that may (409) that has not
    ended (409), from a web session when the run is on a worker that takes runs dispatched from the web only."""
    run_access.refuse_onward(user, True)
    async with engine.begin() as conn:
        access, row = await owned_run(conn, user, project, run_id, "change the access of")
        plan_id, key = row[3], row[4]
        r = tables.runs
        now = bool((await conn.execute(select(r.c.as_owner).where(r.c.id == run_id))).scalar_one())
        if body.as_me == now:
            return await run_view(conn, run_id)
        if body.as_me:
            await let_run_as_owner(conn, user, access, project, run_id, "by=access")
        else:
            await conn.execute(update(r).values(as_owner=False, as_owner_since=None).where(r.c.id == run_id))
            ended = await run_access.end_access(conn, run_id)
            done = run_target(project, plan_id, key, run_id) + f" by=access tokens={ended}"
            await run_access.record(conn, user, access.project_id, done, granted=False)
        view = await run_view(conn, run_id)
    log.info("run access changed", extra={"run_id": run_id, "as_owner": body.as_me, "login": user.login})
    return view


async def let_run_as_owner(conn: AsyncConnection, user: Principal, access, project: str, run_id: int, how: str) -> bool:
    """Let run ``run_id`` of ``project``, which ``user`` dispatched and whose row the caller holds locked, run as them
    (``run_access``), in the caller's transaction; whether its row changed (false when it ran as them already). Only a
    run of a kind that may (409) that has not ended (409), from a web session when the run is on a worker that takes runs
    dispatched from the web only. The audit row run_access.grant ends its target with ``how``, such as ``by=access``
    or ``by=decision:12``."""
    r = tables.runs
    found = select(r.c.as_owner, r.c.state, r.c.kind, r.c.plan_id, r.c.step_key).where(r.c.id == run_id)
    row = (await conn.execute(found)).one()
    if row.as_owner:
        return False
    check_dispatcher(access)
    await check_seat(conn, user, "letting a run run as you")
    from evo_agents.hub.server.changes import curator_policy  # it reads runs through the routes of this package

    curator = await curator_policy(conn, access.project_id, row.kind, row.plan_id) is not None
    refusal = run_access.may_run_as_owner(row.kind, curator)
    if refusal is not None:
        raise HTTPException(409, f"run {run_id} cannot run as you: {refusal}")
    if row.state not in runs.ACTIVE_STATES:
        raise HTTPException(409, f"run {run_id} is {row.state}, which is final: it has no agent to run as you")
    await web_only_steering(conn, user, run_id, "let it run as you", "do it on the web", "changed")
    await conn.execute(update(r).values(as_owner=True, as_owner_since=func.now()).where(r.c.id == run_id))
    done = f"{run_target(project, row.plan_id, row.step_key, run_id)} {how}"
    await run_access.record(conn, user, access.project_id, done, granted=True)
    return True
