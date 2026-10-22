"""The stop of autonomy S4 (plan autonomy-levels, criterion a11), under /v1/autonomy/stops.

- POST /v1/autonomy/stops: stop the runs of a person (``login``), of everyone with a live seat in a unit or a unit
  below it (``unit``), or of the whole hub (``all``). A person stops their own, a lead those of the units they lead
  and below, a hub admin anyone and the hub (``org.stop_refusal``); the agent of a run never does. The stop cancels
  every run of the principals it covers that has not ended, as ``hub run cancel`` does: a queued or parked run, or one
  in review, at once; a held run is asked to stop, and its worker stops it at its next heartbeat. It takes back their
  leases (``lease_ending.end_leases``; the GitHub tokens are revoked once the stop is written). Until it is lifted,
  the hub queues no run of those principals, the runs it makes itself included (``autonomy_hold``). It writes the audit
  row ``autonomy.stop`` and tells each person whose runs it stopped (the notice ``autonomy``). A second stop of the
  same scope while one holds is refused (409).
- POST /v1/autonomy/stops/lift: lift the stop that holds for that scope, which the same people may do; it writes
  ``autonomy.stop.lift`` and tells the same people. No stop holding there is a 404.
- GET /v1/autonomy/stops/preview: what a stop of that scope would cancel now, for the web's confirmation; the same
  people may read it, and it writes nothing.

A local run is only recorded by the hub, which neither cancels it nor holds it.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import org, runs, tables
from evo_agents.hub.server import audit, autonomy_store, lease_ending, org_store
from evo_agents.hub.server.autonomy_hold import LOGIN, UNIT, scope_name
from evo_agents.hub.server.autonomy_page_models import StopPreview
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.notifications import notify, one_line
from evo_agents.hub.server.org_models import LOGIN_PATTERN
from evo_agents.hub.server.run_state import move_run
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)
MAX_REASON_CHARS = 1000
SLUG_PATTERN = org.SLUG_PATTERN
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
BY = "autonomy-stop"  # how the audit rows of the leases a stop ends name it


class StopSet(BaseModel):
    """What to stop, or lift the stop of: a person, a unit and those below it, or the whole hub."""

    scope: Literal["login", "unit", "all"] = Field(description="login: a person; unit: a unit and below; all: the hub")
    login: Annotated[str | None, Field(pattern=LOGIN_PATTERN)] = Field(None, description="the person, for login")
    unit: Annotated[str | None, Field(pattern=SLUG_PATTERN)] = Field(None, description="the unit's slug, for unit")
    reason: str | None = Field(None, max_length=MAX_REASON_CHARS, description="why; a lift takes none")

    @model_validator(mode="after")
    def _named(self) -> StopSet:
        if (self.scope == LOGIN) != (self.login is not None) or (self.scope == UNIT) != (self.unit is not None):
            raise ValueError("scope login names a login, scope unit a unit, scope all neither")
        return self


class StopView(BaseModel):
    """A stop of autonomy, as written or lifted."""

    id: int
    scope: Literal["login", "unit", "all"]
    target: str = Field(description="login:<login>, unit:<slug> or all")
    reason: str | None
    stopped_by: str = Field(description="the login of who stopped")
    stopped_at: datetime
    lifted_by: str | None = Field(description="the login of who lifted it; null while it holds")
    lifted_at: datetime | None
    runs: list[int] = Field(
        default_factory=list, description="the runs this call cancelled or asked to stop; empty for a lift"
    )
    leases: int = Field(0, description="how many leases this call ended")
    told: list[str] = Field(default_factory=list, description="the logins told")


router = APIRouter(prefix="/v1/autonomy/stops", tags=["autonomy"], responses={401: {"model": ErrorBody}})


async def _subject(conn: AsyncConnection, body: StopSet, user: Principal):
    """(user_id, unit) the stop names, once ``user`` may stop or lift it; 403, 404 or 422 otherwise."""
    if user.scope is not None:
        raise HTTPException(403, "the agent of a run never stops autonomy nor lifts a stop")
    chart, day = await org_store.load(conn), await org_store.today(conn)
    user_id, unit, login = None, None, body.login
    if body.scope == LOGIN:
        found = await org_store.user_by_login(conn, body.login)
        if found is None:
            raise HTTPException(404, f"no user {body.login} on this hub")
        user_id, login = found.id, found.login
    elif body.scope == UNIT:
        unit = chart.unit_by_slug(body.unit)
        if unit is None:
            raise HTTPException(404, f"no unit {body.unit} in the org chart")
    refused = org.stop_refusal(chart, autonomy_store.actor(user), day, user_id=user_id, login=login, unit=unit)
    if refused is not None:
        raise HTTPException(403, refused)
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
    r, cl = tables.runs, tables.credential_leases
    query = select(r.c.id, r.c.state, r.c.cancel_requested_at, r.c.principal_id).where(
        r.c.state.not_in(runs.TERMINAL_STATES), r.c.kind != "local"
    )
    if covered is not None:
        query = query.where(r.c.principal_id.in_(covered))
    rows = (await conn.execute(query.order_by(r.c.id).with_for_update(of=r))).all()
    if not rows:
        return [], 0
    open_leases = select(func.count()).where(cl.c.run_id.in_([row.id for row in rows]), cl.c.revoked_at.is_(None))
    leases = (await conn.execute(open_leases)).scalar_one()
    for row in rows:
        if row.state in runs.HELD_STATES:
            if row.cancel_requested_at is None:
                await conn.execute(update(r).values(cancel_requested_at=func.now()).where(r.c.id == row.id))
            await lease_ending.end_leases(conn, run_id=row.id, actor_id=user.user_id, token_id=user.token_id, by=BY)
        else:
            await move_run(conn, row.id, row.state, "cancelled", "owner", reason=why, token_id=user.token_id)
    return rows, leases


async def _tell(conn: AsyncConnection, people: set[int], title: str, body: str) -> list[str]:
    """Tell ``people`` (the notice ``autonomy``); their logins."""
    for user_id in sorted(people):
        await notify(
            conn,
            user_id=user_id,
            kind="notice",
            notice_kind=runs.AUTONOMY_DECISION_CATEGORY,
            title=one_line(title),
            body=body,
        )
    if not people:
        return []
    u = tables.users
    return sorted((await conn.execute(select(u.c.login).where(u.c.id.in_(people)))).scalars())


async def _live(conn: AsyncConnection, scope: str, user_id: int | None, unit: org.Unit | None):
    s = tables.autonomy_stops
    query = select(s.c.id).where(s.c.scope == scope, s.c.lifted_at.is_(None))
    if user_id is not None:
        query = query.where(s.c.user_id == user_id)
    if unit is not None:
        query = query.where(s.c.unit_id == unit.id)
    return (await conn.execute(query.order_by(s.c.id).limit(1))).scalar_one_or_none()


async def _view(conn: AsyncConnection, stop_id: int, **extra) -> StopView:
    s, by, lifter, units, named = (
        tables.autonomy_stops,
        tables.users.alias("by"),
        tables.users.alias("lifter"),
        tables.org_units,
        tables.users.alias("named"),
    )
    query = (
        select(s, by.c.login.label("by_login"), lifter.c.login.label("lifter_login"), units.c.slug, named.c.login)
        .select_from(
            s.join(by, by.c.id == s.c.stopped_by)
            .outerjoin(lifter, lifter.c.id == s.c.lifted_by)
            .outerjoin(units, units.c.id == s.c.unit_id)
            .outerjoin(named, named.c.id == s.c.user_id)
        )
        .where(s.c.id == stop_id)
    )
    row = (await conn.execute(query)).one()
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
    user_id, unit, chart, day = await _subject(conn, body, user)
    if await _live(conn, body.scope, user_id, unit) is not None:
        raise HTTPException(409, f"a stop of {body.scope} {body.login or body.unit or ''} holds already".rstrip())
    s = tables.autonomy_stops
    values = {"scope": body.scope, "user_id": user_id, "unit_id": None if unit is None else unit.id}
    values |= {"reason": body.reason, "stopped_by": user.user_id}
    stop_id = (await conn.execute(insert(s).values(**values).returning(s.c.id))).scalar_one()
    named = scope_name(body.scope, body.login, None if unit is None else unit.slug)
    why = f"{user.login} stopped autonomy of {named} (stop #{stop_id})" + (f": {body.reason}" if body.reason else "")
    covered = _covered(chart, day, user_id, unit)
    swept, leases = await _sweep(conn, user, covered, why)
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.AUTONOMY_STOP,
        target=f"stop:{stop_id} {named} runs={len(swept)} leases={leases}",
    )
    people = (covered or set()) | {row.principal_id for row in swept if row.principal_id is not None}
    text = f"{why}. Your runs were cancelled, and no new run of yours is queued until the stop is lifted."
    told = await _tell(conn, people, f"Autonomy stopped: {named}", text)
    return await _view(conn, stop_id, runs=[row.id for row in swept], leases=leases, told=told)


async def lift(conn: AsyncConnection, user: Principal, body: StopSet) -> StopView:
    """Lift the stop that holds for the scope ``body`` names, as the module says."""
    await org_store.lock(conn)
    user_id, unit, chart, day = await _subject(conn, body, user)
    stop_id = await _live(conn, body.scope, user_id, unit)
    if stop_id is None:
        raise HTTPException(404, f"no stop of {body.scope} {body.login or body.unit or ''} holds".rstrip())
    s = tables.autonomy_stops
    await conn.execute(update(s).values(lifted_by=user.user_id, lifted_at=func.now()).where(s.c.id == stop_id))
    named = scope_name(body.scope, body.login, None if unit is None else unit.slug)
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.AUTONOMY_STOP_LIFT,
        target=f"stop:{stop_id} {named}",
    )
    text = f"{user.login} lifted stop #{stop_id} of {named}: runs of yours are queued again."
    told = await _tell(conn, _covered(chart, day, user_id, unit) or set(), f"Autonomy stop lifted: {named}", text)
    return await _view(conn, stop_id, told=told)


async def _revoke(request: Request, run_ids: list[int]) -> None:
    """Revoke at GitHub the tokens of the leases the stop ended, once it is written."""
    state = request.app.state
    for run_id in run_ids:
        try:
            await lease_ending.revoke_tokens(state.engine, state.sealer, lease_ending.revoker(state), run_id=run_id)
        except Exception as exc:  # the token ends at its expiry; the next pass of the reaper tries again
            log.warning("tokens of a stopped run not revoked", extra={"run_id": run_id, "error": str(exc)[:200]})


@router.post("", response_model=StopView, status_code=201, responses=REFUSALS)
async def stop_autonomy(request: Request, body: StopSet, user: CurrentUser) -> StopView:
    """Stop the runs of a person, a unit or the hub, and hold new ones until the stop is lifted."""
    async with request.app.state.engine.begin() as conn:
        view = await stop(conn, user, body)
    await _revoke(request, view.runs)
    log.info("autonomy stopped", extra={"stop": view.id, "target": view.target, "runs": len(view.runs)})
    return view


@router.post("/lift", response_model=StopView, responses=REFUSALS)
async def lift_stop(request: Request, body: StopSet, user: CurrentUser) -> StopView:
    """Lift the stop that holds for a person, a unit or the hub."""
    async with request.app.state.engine.begin() as conn:
        view = await lift(conn, user, body)
    log.info("autonomy stop lifted", extra={"stop": view.id, "target": view.target})
    return view


async def preview(conn: AsyncConnection, user: Principal, body: StopSet) -> StopPreview:
    """What the stop ``body`` names would cancel now, once ``user`` may write it."""
    user_id, unit, chart, day = await _subject(conn, body, user)
    covered = _covered(chart, day, user_id, unit)
    r = tables.runs
    query = select(func.count()).where(r.c.state.not_in(runs.TERMINAL_STATES), r.c.kind != "local")
    if covered is not None:
        query = query.where(r.c.principal_id.in_(covered))
    return StopPreview(
        target=scope_name(body.scope, body.login, None if unit is None else unit.slug),
        runs=(await conn.execute(query)).scalar_one(),
        people=None if covered is None else len(covered),
        holds=await _live(conn, body.scope, user_id, unit) is not None,
    )


@router.get("/preview", response_model=StopPreview, responses=REFUSALS)
async def preview_stop(
    request: Request,
    user: CurrentUser,
    scope: Literal["login", "unit", "all"],
    login: str | None = Query(None, max_length=100),
    unit: str | None = Query(None, max_length=63),
) -> StopPreview:
    """How many runs and people a stop of that scope would cover now, and whether one holds already."""
    try:
        body = StopSet(scope=scope, login=login, unit=unit)
    except ValidationError as exc:
        raise HTTPException(422, "; ".join(error["msg"] for error in exc.errors())) from exc
    async with request.app.state.engine.connect() as conn:
        return await preview(conn, user, body)
