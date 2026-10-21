"""What the owner's answer to the decision of a stopped release cycle does (``release_cycles.ask_owner`` asks it;
``decisions.answer_decision`` answers it as any decision, and hands the option here).

- ``continue``: the cycle is open again with one more cap of its schedule on its own, and goes on from where it
  stopped: the CI of its newest tag is followed again from now when it was pending, running or overdue; a red newest
  tag gets a new fix run; a cycle with no tag of its own yet gets a new cut run (``release_cycles.queue_run``, which
  stops it again when even the new cap is spent). Nothing is queued while a release run of the cycle is active.
- ``close``: the cycle is closed, its reason kept.
- ``pause``: the schedule is paused as ``evo-agents hub release pause`` pauses it (its queued release runs cancelled),
  and the cycle closed with that reason.

The answer needs the admin role on the project, as the schedule does; a cycle no longer stopped takes no answer (409).
The hub says what it did in the cycle's thread of the triage chat.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import curator, release_ci, releases, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server.chat_runs import hub_reply
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.release_check import cancel_queued
from evo_agents.hub.server.release_cycles import cycle_of, queue_run
from evo_agents.hub.server.security import Principal
from evo_agents.hub.tables import RUN_ACTIVE

log = logging.getLogger(__name__)


async def _newest(conn: AsyncConnection, found):
    """The tag of highest version cycle ``found`` follows, or None."""
    t = tables.release_tags
    prefix = releases.tag_prefix(found.tag_pattern) + "*"
    rows = (await conn.execute(select(t).where(t.c.cycle_id == found.id))).all()
    versioned = [(releases.tag_version(row.tag, prefix) or (-1, -1, -1), row.id, row) for row in rows]
    return max(versioned, key=lambda item: item[:2])[2] if versioned else None


async def _go_on(conn: AsyncConnection, found) -> str:
    c, rr, r, t = tables.release_cycles, tables.release_runs, tables.runs, tables.release_tags
    cap = float(found.budget_usd) + float(found.schedule_cap)
    await conn.execute(
        update(c)
        .where(c.c.id == found.id)
        .values(state="open", budget_usd=cap, stop_reason=None, stop_cause=None, updated_at=func.now())
    )
    said = f"cycle #{found.id} goes on with a cap of {curator.money(cap)}"
    active = exists().where(rr.c.cycle_id == found.id, r.c.id == rr.c.run_id, r.c.state.in_(RUN_ACTIVE))
    if (await conn.execute(select(active))).scalar_one():
        return said + ": a run of it is still active"
    tag = await _newest(conn, found)
    if tag is not None and tag.ci_state in (*release_ci.WATCHED, "timeout"):
        values = {"ci_state": "running" if tag.workflow_run_id else "pending", "watched_since": func.now()}
        await conn.execute(update(t).where(t.c.id == tag.id).values(**values, problem=None))
        return said + f": the hub follows the CI of {tag.tag} again from now"
    mode, red = ("fix", tag.tag) if tag is not None and tag.ci_state == "failed" else ("cut", None)
    run_id = await queue_run(conn, found.id, mode, red_tag=red)
    if run_id is None:
        return said + f", but no {mode} run could be queued: see the cycle"
    return said + f": run #{run_id} " + (f"fixes {red} forward" if red else f"cuts {found.target_ref} again")


async def _close(conn: AsyncConnection, found, why: str) -> None:
    c = tables.release_cycles
    reason = releases.clip_reason(f"{found.stop_reason or 'stopped'}; {why}")
    await conn.execute(
        update(c)
        .where(c.c.id == found.id)
        .values(state="closed", stop_reason=reason, ended_at=func.now(), updated_at=func.now())
    )


async def _pause(conn: AsyncConnection, found, user: Principal) -> str:
    s = tables.release_schedules
    await conn.execute(
        update(s)
        .values(paused_at=func.now(), paused_by=user.user_id, updated_at=func.now())
        .where(s.c.id == found.schedule_id, s.c.paused_at.is_(None))
    )
    why = f"the release schedule of {found.project} was paused by {user.login}"
    cancelled = await cancel_queued(conn, found.project_id, why)
    await _close(conn, found, why)
    return f"{why}, and cycle #{found.id} closed" + (f"; runs {cancelled} cancelled" if cancelled else "")


async def answer_stop(conn: AsyncConnection, user: Principal, decision_id: int, option: str) -> str:
    """Do what ``option`` of decision ``decision_id`` says for the cycle that asked it (see the module), in the
    caller's transaction; what the hub did, in words."""
    c = tables.release_cycles
    query = select(c.c.id, c.c.state).where(c.c.decision_id == decision_id).with_for_update()
    locked = (await conn.execute(query)).one_or_none()
    if locked is None or locked.state != "stopped":
        state = locked.state if locked else "gone"
        raise HTTPException(409, f"decision {decision_id} is about a release cycle that is {state}, not stopped")
    found = await cycle_of(conn, locked.id)
    access = await project_access(conn, user, found.project)
    if not has_role(access.role, "admin"):
        raise HTTPException(403, f"answering for the release of {found.project} needs the admin role on it")
    if option == "continue":
        done = await _go_on(conn, found)
    elif option == "close":
        await _close(conn, found, f"closed by {user.login}")
        done = f"cycle #{found.id} is closed"
    elif option == "pause":
        done = await _pause(conn, found, user)
    else:
        raise HTTPException(422, f"decision {decision_id} takes continue, close or pause, not {option!r}")
    if found.triage_chat_id and found.triage_root_id:
        await hub_reply(conn, found.triage_chat_id, found.triage_root_id, f"{user.login} chose {option}: {done}.")
    log.info("release stop answered", extra={"cycle_id": found.id, "option": option, "login": user.login})
    return done
