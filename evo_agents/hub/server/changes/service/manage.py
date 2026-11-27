"""The list of a project's changes of the Curator, an admin's close of a change left open with no pull request, and
the drop of a change by its own Builder before its pull request opens (the routes:
``evo_agents.hub.server.changes.routes``)."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub.server import audit
from evo_agents.hub.server.changes.models.views import Change, ChangeClose, ChangeDrop, ChangeList
from evo_agents.hub.server.changes.repository import judge as store
from evo_agents.hub.server.changes.repository.views import change_of_plan, change_view, with_repos
from evo_agents.hub.server.changes.service.moves import set_change
from evo_agents.hub.server.forge.models.protection import reason_line
from evo_agents.hub.server.forge.service.protection import need_reader
from evo_agents.hub.server.plans import service as plan_service
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.security import MACHINE, Principal, holds

log = logging.getLogger(__name__)

CLOSE = "curator.close"  # an admin closed a change with no pull request: "<project> change:<id> proposal:<id>"
MAX_LIST = 200
DROPPABLE = ("planned", "pr_pending")  # a change its Builder may drop: no pull request yet, no Judge


async def list_changes(engine: AsyncEngine, user: Principal, project: str) -> ChangeList:
    """The project's Curator changes, newest first, with the plans the caller may read."""
    async with engine.begin() as conn:
        access = await project_access(conn, user, project)
        need_reader(access)
        rows = await store.project_changes(conn, access.project_id, MAX_LIST)
        from evo_agents.hub.server.runs.service.views import visible_plans

        readable = set(await visible_plans(conn, access, None))
        through = plan_service.read_sink(access, None) or ""  # no hub sink: no sink lets it through
        shown = [
            Change(**{key: value for key, value in row._mapping.items() if key != "proposal_label"})
            for row in rows
            if access.visible(row.proposal_label, through) and (row.plan_id is None or row.plan_id in readable)
        ]
        shown = await with_repos(conn, shown)
    return ChangeList(project=access.name, changes=shown)


async def close_change(engine: AsyncEngine, user: Principal, project: str, change_id: int, body: ChangeClose) -> Change:
    """Close a change left open with no pull request, saying why; the admins of the project alone may."""
    why = reason_line(body.reason)
    if why is None:
        raise HTTPException(422, "say why the change is closed: the reason is empty")
    async with engine.begin() as conn:
        access = await project_access(conn, user, project)
        need_reader(access)
        if not holds(access.role, "admin"):
            raise HTTPException(
                403, f"closing a change of project {access.name} needs the admin role on it; you hold {access.role}"
            )
        row = await store.locked_change(conn, change_id, access.project_id)
        from evo_agents.hub.server.runs.service.views import visible_plans

        if (
            row is None
            or not access.visible(row.proposal_label, plan_service.read_sink(access, None) or "")
            or (row.plan_id is not None and row.plan_id not in set(await visible_plans(conn, access, None)))
        ):
            raise HTTPException(404, f"no change #{change_id} of the Curator in project {access.name} you may read")
        if row.state != "open":
            raise HTTPException(
                409, f"change #{change_id} is {row.state}: only a change left open is closed by hand; nothing changed"
            )
        if row.pr_number is not None:
            raise HTTPException(
                409,
                f"change #{change_id} has pull request {row.pr_url or row.pr_number}: close that on GitHub and the "
                "hub closes the change when it reads it again; nothing changed",
            )
        await set_change(
            conn, change_id, state="closed", reason=why, ledger_extra={"actor_id": user.user_id, "login": user.login}
        )
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=CLOSE,
            target=f"{access.name} change:{change_id} proposal:{row.proposal_id}",
            project_id=access.project_id,
        )
        view = await change_view(conn, change_id)
    log.info("curator change closed by hand", extra={"change_id": change_id, "login": user.login})
    return view


async def drop_change(engine: AsyncEngine, user: Principal, run_id: int, body: ChangeDrop) -> Change:
    """Close the change whose plan the plan run ``run_id`` of the worker of ``user`` builds, before its pull request
    opens, saying why, for the member who dispatched the run, an admin of the project."""
    from evo_agents.hub.server.runs.service.plan_runs import held_plan_run

    why = reason_line(body.reason)
    if why is None:
        raise HTTPException(422, "say why the change is dropped: the reason is empty")
    note = (body.note or "").strip() or None
    async with engine.begin() as conn:
        _, row = await held_plan_run(conn, user, run_id, lock=True)
        change = await change_of_plan(conn, row.project_id, row.plan_id, lock=True)
        if change is None:
            raise HTTPException(
                404,
                f"run {run_id} runs plan {row.plan_id}, which is no plan of the Curator: only the Builder of a "
                "Curator's change drops it",
            )
        owner = Principal(row.dispatched_by, row.login, False, user.token_id, MACHINE, "")
        access = await project_access(conn, owner, row.name)
        if not holds(access.role, "admin"):
            raise HTTPException(
                403,
                f"dropping change #{change.id} closes it for {row.login}, who dispatched run {run_id}, and closing a "
                f"change of project {access.name} needs the admin role on it; {row.login} holds {access.role}",
            )
        if change.state not in DROPPABLE or change.pr_number is not None:
            pull = f" with pull request {change.pr_url or change.pr_number}" if change.pr_number is not None else ""
            raise HTTPException(
                409,
                f"change #{change.id} is {change.state}{pull}: its Builder drops it only while it is planned or waits "
                "for its pull request; nothing changed",
            )
        extra = {"actor": "agent", "run_id": run_id, "actor_id": row.dispatched_by, "login": row.login, "note": note}
        await set_change(conn, change.id, state="closed", reason=why, builder_run_id=run_id, ledger_extra=extra)
        text = f"Run {run_id} dropped the Curator's change #{change.id} for {row.login}: {why}."
        await write_event(conn, run_id, {"text": text, "change_id": change.id})
        await audit.record(
            conn,
            actor_id=row.dispatched_by,
            token_id=user.token_id,
            action=CLOSE,
            target=f"{access.name} change:{change.id} proposal:{change.proposal_id} run:{run_id}",
            project_id=access.project_id,
        )
        view = await change_view(conn, change.id)
    log.info("curator change dropped by its builder", extra={"change_id": change.id, "run_id": run_id})
    return view
