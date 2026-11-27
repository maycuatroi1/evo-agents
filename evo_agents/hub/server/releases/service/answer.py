"""What the owner's answer to the decision of a stopped release cycle does (``service.cycles.ask_owner`` asks it;
``decisions.answer_decision`` answers it as any decision, and hands the option here).

- ``continue``: the cycle is open again and goes on from where it stopped: the CI of its newest tag is followed again
  from now when it was pending, running or overdue; a red newest tag gets a new fix run; a cycle with no tag of its own
  yet gets a new cut run (``service.cycles.go_on``). Nothing is queued while a release run of the cycle is active, nor
  while the branch a refused landing left is not settled (``service.cycles.left_behind``): ``service.settle`` tags a
  merge of it a person made, or deletes it and then queues the run.
- ``close``: the cycle is closed, its reason kept.
- ``pause``: the schedule is paused as ``evo-agents hub release pause`` pauses it (its queued release runs cancelled),
  and the cycle closed with that reason.

The answer needs the admin role on the project, as the schedule does; a cycle no longer stopped takes no answer (409).
The hub says what it did in the cycle's thread of the triage chat.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import releases
from evo_agents.hub.server.chats.service.runs import hub_reply
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.releases.repository import cycles as cycles_repo
from evo_agents.hub.server.releases.repository import schedules as schedules_repo
from evo_agents.hub.server.releases.repository.cycles import cycle_of
from evo_agents.hub.server.releases.service.check import cancel_queued
from evo_agents.hub.server.releases.service.cycles import go_on, held_by
from evo_agents.hub.server.security import Principal, holds

log = logging.getLogger(__name__)


async def _go_on(conn: AsyncConnection, found) -> str:
    await cycles_repo.reopen(conn, found.id)
    said = f"cycle #{found.id} goes on"
    if await cycles_repo.run_active(conn, found.id):
        return said + ": a run of it is still active"
    left = await held_by(conn, found)
    if left is not None:
        return said + (
            f": the hub first settles {left.branch} of run #{left.run_id} (it tags a merge of it a person made, or "
            "deletes the branch and then queues the next run)"
        )
    return said + ": " + await go_on(conn, found)


async def _close(conn: AsyncConnection, found, why: str) -> None:
    reason = releases.clip_reason(f"{found.stop_reason or 'stopped'}; {why}")
    await cycles_repo.close(conn, found.id, reason)


async def _pause(conn: AsyncConnection, found, user: Principal) -> str:
    await schedules_repo.pause(conn, found.schedule_id, user.user_id)
    why = f"the release schedule of {found.project} was paused by {user.login}"
    cancelled = await cancel_queued(conn, found.project_id, why)
    await _close(conn, found, why)
    return f"{why}, and cycle #{found.id} closed" + (f"; runs {cancelled} cancelled" if cancelled else "")


async def answer_stop(conn: AsyncConnection, user: Principal, decision_id: int, option: str) -> str:
    """Do what ``option`` of decision ``decision_id`` says for the cycle that asked it (see the module), in the
    caller's transaction; what the hub did, in words."""
    locked = await cycles_repo.asked_by(conn, decision_id)
    if locked is None or locked.state != "stopped":
        state = locked.state if locked else "gone"
        raise HTTPException(409, f"decision {decision_id} is about a release cycle that is {state}, not stopped")
    found = await cycle_of(conn, locked.id)
    access = await project_access(conn, user, found.project)
    if not holds(access.role, "admin"):
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
