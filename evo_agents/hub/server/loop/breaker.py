"""The breaker of a project's Loop (plan loop-engine, criterion a7; the report's section Triage).

``check``, a step of each tick: when the work items the Loop made in the last hour (counted from its last reset when
that is later) are more than the policy's ``loop.max_items_per_hour``, the breaker trips (loop_states.breaker
``tripped``, why and when): the tick reads no event past its cursor, so none is lost, and hands out no run; the runs in
flight go on. Each admin of the project gets the notice ``loop_paused`` in the Inbox and on the channels they linked
(Telegram among them, as any notice), and the trip is audited as ``loop.breaker``, with no actor.

``resume``: an admin of the project alone resets it (POST /v1/projects/{p}/loop/resume, ``evo-agents hub loop
resume``), audited as ``loop.resume``; the notices of the trip are read then and a tick is queued. Turning
``loop.enabled`` off is something else: it cancels every work item not finished.

``status`` is what GET /v1/projects/{p}/loop shows of it.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import loop_jobs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit
from evo_agents.hub.server.loop.model import View

log = logging.getLogger(__name__)

BREAKER_STATES = ARMED, TRIPPED = ("armed", "tripped")  # loop_states.breaker
NOTICE = "loop_paused"  # runs.NOTICE_KINDS
BREAKER = "loop.breaker"  # the breaker of a project's Loop tripped: "<project> items=<n> max=<cap>"
RESUME = "loop.resume"  # an admin reset it: "<project>"
WINDOW = timedelta(hours=1)


def loop_link(project: str) -> str:
    """The web page of a project's Loop, as its notices link to it."""
    return f"/p/{project}/loop"


async def _admins(conn: AsyncConnection, project_id: int) -> list[int]:
    g = tables.grants
    query = select(g.c.user_id).where(g.c.project_id == project_id, g.c.role == "admin").order_by(g.c.user_id)
    return list(await conn.scalars(query))


async def made_lately(conn: AsyncConnection, project_id: int, view: View, resumed_at) -> int:
    """The work items of the project made in the last hour, since ``resumed_at`` when that is later."""
    since = view.now - WINDOW
    if resumed_at is not None and resumed_at > since:
        since = resumed_at
    i = tables.loop_items
    query = select(func.count()).where(i.c.project_id == project_id, i.c.created_at > since)
    return int(await conn.scalar(query) or 0)


async def _tell(conn: AsyncConnection, project_id: int, project: str, made: int, cap: int, reason: str) -> None:
    from evo_agents.hub.server.notifications import notify  # it reaches the plugins' channels

    body = "\n".join(
        [
            f"The Loop of project {project} made {made} work items in an hour, over loop.max_items_per_hour ({cap}): "
            "its breaker tripped.",
            "",
            "It reads no new event and hands out no run until an admin resets it, with `evo-agents hub loop resume "
            f"--project {project}` or on the Loop page; the runs in flight go on, and the events wait past its cursor.",
        ]
    )
    for user_id in await _admins(conn, project_id):
        await notify(
            conn,
            user_id=user_id,
            kind="notice",
            notice_kind=NOTICE,
            project_id=project_id,
            title=f"The Loop of {project} stopped: {made} work items in an hour",
            body=body,
            details={"items": made, "max_items_per_hour": cap, "reason": reason},
            link=loop_link(project),
        )


async def check(conn: AsyncConnection, project_id: int, view: View) -> bool:
    """Trip the breaker of the project's Loop when it made too many work items (see the module), in the tick's
    transaction, which holds its loop_states row; whether it tripped now."""
    s, p = tables.loop_states, tables.projects
    row = (await conn.execute(select(s.c.breaker, s.c.resumed_at).where(s.c.project_id == project_id))).first()
    if row is None or row.breaker != ARMED:
        return False
    cap = int(view.policy["max_items_per_hour"])
    made = await made_lately(conn, project_id, view, row.resumed_at)
    if made <= cap:
        return False
    reason = f"{made} work items in an hour, over loop.max_items_per_hour {cap}"
    values = {"breaker": TRIPPED, "breaker_reason": reason, "tripped_at": func.now(), "updated_at": func.now()}
    await conn.execute(update(s).where(s.c.project_id == project_id).values(values))
    project = await conn.scalar(select(p.c.name).where(p.c.id == project_id))
    await _tell(conn, project_id, project, made, cap, reason)
    target = f"{project} items={made} max={cap}"
    await audit.record(conn, actor_id=None, token_id=None, action=BREAKER, target=target, project_id=project_id)
    log.warning("loop breaker tripped", extra={"project_id": project_id, "items": made, "max_items_per_hour": cap})
    return True


async def resume(conn: AsyncConnection, user, access) -> None:
    """An admin of the project resets the breaker of its Loop; 403 for anyone else, 409 when it has not tripped."""
    if user.acts_for_run is not None or not has_role(access.role, "admin"):
        raise HTTPException(403, f"resetting the breaker of the Loop needs the admin role on project {access.name}")
    s, n = tables.loop_states, tables.notifications
    query = select(s.c.breaker).where(s.c.project_id == access.project_id).with_for_update()
    breaker = await conn.scalar(query)
    if breaker != TRIPPED:
        raise HTTPException(409, f"the breaker of the Loop of project {access.name} has not tripped")
    values = {"breaker": ARMED, "resumed_by": user.user_id, "resumed_at": func.now(), "updated_at": func.now()}
    await conn.execute(update(s).where(s.c.project_id == access.project_id).values(values))
    told = (n.c.project_id == access.project_id) & (n.c.notice_kind == NOTICE) & n.c.read_at.is_(None)
    await conn.execute(update(n).values(read_at=func.now()).where(told))
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=RESUME,
        target=access.name,
        project_id=access.project_id,
    )
    await loop_jobs.defer_tick(conn, access.project_id)
    log.info("loop breaker reset", extra={"project": access.name, "login": user.login})


async def status(conn: AsyncConnection, project_id: int) -> dict:
    """The project's Loop as GET .../loop shows it: on or off, its breaker, cursor and last tick, and its work items
    by state."""
    s, u, i = tables.loop_states, tables.users, tables.loop_items
    query = (
        select(s, u.c.login.label("resumed_login"))
        .join_from(s, u, u.c.id == s.c.resumed_by, isouter=True)
        .where(s.c.project_id == project_id)
    )
    row = (await conn.execute(query)).first()
    counts = select(i.c.state, func.count()).where(i.c.project_id == project_id).group_by(i.c.state)
    found = {
        "enabled": bool(row and row.enabled),
        "breaker": row.breaker if row else ARMED,
        "breaker_reason": row.breaker_reason if row else None,
        "tripped_at": row.tripped_at if row else None,
        "resumed_by": row.resumed_login if row else None,
        "resumed_at": row.resumed_at if row else None,
        "cursor": row.cursor if row else 0,
        "ticked_at": row.ticked_at if row else None,
    }
    return {**found, "items": {state: count for state, count in await conn.execute(counts)}}


__all__ = ["ARMED", "BREAKER_STATES", "NOTICE", "TRIPPED", "check", "loop_link", "made_lately", "resume", "status"]
