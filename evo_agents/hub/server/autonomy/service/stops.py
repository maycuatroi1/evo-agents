"""The stop of autonomy S4 (plan autonomy-levels, criterion a11): who may stop or lift the runs of a person, a unit
and those below it, or the hub (``org.stop_refusal``); writing a stop, which cancels the runs of the principals it
covers and ends their leases, and lifting it, each audited and told to those people; and what a stop would cancel
now. The routes are ``autonomy.routes.stops``; what a stop holds is ``autonomy.service.hold``.
"""

from __future__ import annotations

import logging

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import org, runs
from evo_agents.hub.server import audit, lease_ending, org_store
from evo_agents.hub.server.autonomy.models.page import StopPreview
from evo_agents.hub.server.autonomy.models.stops import LOGIN, UNIT, StopSet, StopView, scope_name
from evo_agents.hub.server.autonomy.repository import stops as store
from evo_agents.hub.server.autonomy.service import levels as autonomy_store
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, Forbidden, NotFound, Unprocessable
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.notifications.service.notifier import NOTIFIER
from evo_agents.hub.server.run_state import move_run
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)
BY = "autonomy-stop"  # how the audit rows of the leases a stop ends name it


def stop_set(scope: str, login: str | None, unit: str | None) -> StopSet:
    """The stop a preview's query names; 422 with pydantic's messages when it names it wrong."""
    try:
        return StopSet(scope=scope, login=login, unit=unit)  # pyright: ignore[reportCallIssue]
    except ValidationError as exc:
        raise Unprocessable("; ".join(error["msg"] for error in exc.errors())) from exc


async def subject(conn: AsyncConnection, body: StopSet, user: Principal):
    """(user_id, unit) the stop names, once ``user`` may stop or lift it; 403, 404 or 422 otherwise."""
    if user.acts_for_run is not None:
        raise Forbidden("the agent of a run never stops autonomy nor lifts a stop")
    chart, day = await org_store.load(conn), await org_store.today(conn)
    user_id, unit, login = None, None, body.login
    if body.scope == LOGIN:
        found = await org_store.user_by_login(conn, body.login)  # pyright: ignore[reportArgumentType]
        if found is None:
            raise NotFound(f"no user {body.login} on this hub")
        user_id, login = found.id, found.login
    elif body.scope == UNIT:
        unit = chart.unit_by_slug(body.unit)  # pyright: ignore[reportArgumentType]
        if unit is None:
            raise NotFound(f"no unit {body.unit} in the org chart")
    refused = org.stop_refusal(chart, autonomy_store.actor(user), day, user_id=user_id, login=login, unit=unit)
    if refused is not None:
        raise Forbidden(refused)
    return user_id, unit, chart, day


def _covered(chart: org.Chart, day, user_id: int | None, unit: org.Unit | None) -> set[int] | None:
    """The principals the stop covers; None for the whole hub."""
    if user_id is not None:
        return {user_id}
    if unit is None:
        return None
    return {
        seat.user_id
        for seat in chart.seats.values()
        if seat.user_id is not None and not chart.ended(seat, day) and chart.within(seat.unit_id, unit.id)
    }


async def _sweep(conn: AsyncConnection, user: Principal, covered: set[int] | None, why: str) -> tuple[list, int]:
    """Cancel the runs of ``covered`` that have not ended, as ``controls.cancel_run`` does, and end their leases; the
    runs (id, principal) and how many leases ended."""
    rows = await store.open_runs(conn, covered)
    if not rows:
        return [], 0
    leases = await store.count_open_leases(conn, [row.id for row in rows])
    for row in rows:
        if row.state in runs.HELD_STATES:
            if row.cancel_requested_at is None:
                await store.ask_cancel(conn, row.id)
            await lease_ending.end_leases(conn, run_id=row.id, actor_id=user.user_id, token_id=user.token_id, by=BY)
        else:
            await move_run(conn, row.id, row.state, "cancelled", "owner", reason=why, token_id=user.token_id)
    return rows, leases  # pyright: ignore[reportReturnType]


async def _tell(conn: AsyncConnection, people: set[int], title: str, body: str) -> list[str]:
    """Tell ``people`` (the notice ``autonomy``); their logins."""
    for user_id in sorted(people):
        await NOTIFIER.notify(
            conn,
            user_id=user_id,
            kind="notice",
            notice_kind=runs.AUTONOMY_DECISION_CATEGORY,
            title=NOTIFIER.one_line(title),
            body=body,
        )
    if not people:
        return []
    return await store.logins(conn, people)


async def _live(conn: AsyncConnection, scope: str, user_id: int | None, unit: org.Unit | None) -> int | None:
    return await store.live_id(conn, scope, user_id, None if unit is None else unit.id)


async def _view(conn: AsyncConnection, stop_id: int, **extra) -> StopView:
    row = await store.stop_row(conn, stop_id)
    return StopView(
        id=row.id,
        scope=row.scope,
        target=scope_name(row.scope, row.login, row.slug),
        reason=row.reason,
        stopped_by=row.by_login,
        stopped_at=row.stopped_at,
        lifted_by=row.lifter_login,
        lifted_at=row.lifted_at,
        **extra,
    )


async def stop(conn: AsyncConnection, user: Principal, body: StopSet) -> StopView:
    """Write the stop ``body`` names and sweep the runs it covers, as the module says."""
    await org_store.lock(conn)  # two stops, or a stop and a change of the chart, take turns
    user_id, unit, chart, day = await subject(conn, body, user)
    if await _live(conn, body.scope, user_id, unit) is not None:
        raise Conflict(f"a stop of {body.scope} {body.login or body.unit or ''} holds already".rstrip())
    values = {"scope": body.scope, "user_id": user_id, "unit_id": None if unit is None else unit.id}
    values |= {"reason": body.reason, "stopped_by": user.user_id}
    stop_id = await store.add(conn, values)
    named = scope_name(body.scope, body.login, None if unit is None else unit.slug)
    why = f"{user.login} stopped autonomy of {named} (stop #{stop_id})" + (f": {body.reason}" if body.reason else "")
    covered = _covered(chart, day, user_id, unit)
    swept, leases = await _sweep(conn, user, covered, why)
    target = f"stop:{stop_id} {named} runs={len(swept)} leases={leases}"
    await Auditor(conn, None, user).record(audit.AUTONOMY_STOP, target)
    people = (covered or set()) | {row.principal_id for row in swept if row.principal_id is not None}
    text = f"{why}. Your runs were cancelled, and no new run of yours is queued until the stop is lifted."
    told = await _tell(conn, people, f"Autonomy stopped: {named}", text)
    return await _view(conn, stop_id, runs=[row.id for row in swept], leases=leases, told=told)


async def lift(conn: AsyncConnection, user: Principal, body: StopSet) -> StopView:
    """Lift the stop that holds for the scope ``body`` names, as the module says."""
    await org_store.lock(conn)
    user_id, unit, chart, day = await subject(conn, body, user)
    stop_id = await _live(conn, body.scope, user_id, unit)
    if stop_id is None:
        raise NotFound(f"no stop of {body.scope} {body.login or body.unit or ''} holds".rstrip())
    await store.mark_lifted(conn, stop_id, user.user_id)
    named = scope_name(body.scope, body.login, None if unit is None else unit.slug)
    await Auditor(conn, None, user).record(audit.AUTONOMY_STOP_LIFT, f"stop:{stop_id} {named}")
    text = f"{user.login} lifted stop #{stop_id} of {named}: runs of yours are queued again."
    told = await _tell(conn, _covered(chart, day, user_id, unit) or set(), f"Autonomy stop lifted: {named}", text)
    return await _view(conn, stop_id, told=told)


async def revoke(state, run_ids: list[int]) -> None:
    """Revoke at GitHub the tokens of the leases the stop ended, once it is written; ``state`` is the app's."""
    for run_id in run_ids:
        try:
            await lease_ending.revoke_tokens(state.engine, state.sealer, lease_ending.revoker(state), run_id=run_id)
        except Exception as exc:  # the token ends at its expiry; the next pass of the reaper tries again
            log.warning("tokens of a stopped run not revoked", extra={"run_id": run_id, "error": str(exc)[:200]})


async def preview(conn: AsyncConnection, user: Principal, body: StopSet) -> StopPreview:
    """What the stop ``body`` names would cancel now, once ``user`` may write it."""
    user_id, unit, chart, day = await subject(conn, body, user)
    covered = _covered(chart, day, user_id, unit)
    return StopPreview(
        target=scope_name(body.scope, body.login, None if unit is None else unit.slug),
        runs=await store.count_open_runs(conn, covered),
        people=None if covered is None else len(covered),
        holds=await _live(conn, body.scope, user_id, unit) is not None,
    )


# The routes


async def write_stop(engine: AsyncEngine, user: Principal, body: StopSet) -> StopView:
    """``stop``, in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        return await stop(conn, user, body)


async def lift_stop(engine: AsyncEngine, user: Principal, body: StopSet) -> StopView:
    """``lift``, in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        return await lift(conn, user, body)


async def read_preview(engine: AsyncEngine, user: Principal, body: StopSet) -> StopPreview:
    """``preview``, on a connection of its own that writes nothing."""
    async with engine.connect() as conn:
        return await preview(conn, user, body)
