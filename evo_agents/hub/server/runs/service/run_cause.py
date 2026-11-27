"""Taking back the cause a core plugin gave a failed run (``evo_agents.hub.server.run_cause``): who may, the
classification still in force, and the restore with its run event and audit record, in one transaction."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.server import audit
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs.models.run_cause import CAUSE_KEY, CLASSIFIED, RESTORED, CauseRestored
from evo_agents.hub.server.runs.repository import run_cause as repository
from evo_agents.hub.server.security import Principal, holds


async def classified(conn: AsyncConnection, run_id: int):
    """The classification of run ``run_id`` still in force (the newest of its cause events, a ``classified`` one), or
    None."""
    newest = await repository.newest_cause_event(conn, run_id)
    if newest is None or newest.body[CAUSE_KEY]["step"] != CLASSIFIED:
        return None
    return newest


async def restore(engine: AsyncEngine, user: Principal, project: str, run_id: int) -> CauseRestored:
    """Give failed run ``run_id`` of ``project`` back the cause it had before a core plugin classified it: 403 without
    the writer role, 404 for no such run, 409 when no classification is in force."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        if not holds(access.role, "writer"):
            raise HTTPException(
                403, f"restoring the cause of a run of project {access.name} needs the writer role on it"
            )
        row = await repository.locked_cause(conn, access.project_id, run_id)
        if row is None:
            raise HTTPException(404, f"project {access.name} has no run {run_id}")
        found = await classified(conn, run_id)
        if found is None:
            raise HTTPException(409, f"no plugin's classification of the cause of run {run_id} is in force")
        note = found.body[CAUSE_KEY]
        previous = note["previous_cause"]
        await repository.set_cause(conn, run_id, previous)
        await write_event(
            conn,
            run_id,
            {
                "message": f"{user.login} restored the cause of the run to {previous}, which plugin "
                f"{note['plugin']} had classified.",
                "failure_cause": previous,
                "failure_cause_inferred": True,
                CAUSE_KEY: {"step": RESTORED, "plugin": note["plugin"], "restored_by": user.login, "of": found.seq},
            },
        )
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.RUN_CAUSE,
            target=f"{access.name} run:{run_id} cause={previous} restored from={row.failure_cause}",
            project_id=access.project_id,
        )
    return CauseRestored(run_id=run_id, failure_cause=previous, was=row.failure_cause)
