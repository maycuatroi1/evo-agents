"""Take back the cause a core plugin gave a failed run: ``POST /v1/projects/{project}/runs/{run_id}/cause/restore``
(the writer role). The cause goes back to the one the run had before the plugin classified it (the hub's guess), the
run's log gets a ``system`` event that says who restored it and the audit log gets ``run.cause``. The classification
itself is written by ``evo_agents.hub.server.core_plugin_api`` (``classify_run_failure``).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs.models import MAX_ID
from evo_agents.hub.server.security import CurrentUser

CAUSE_KEY = "cause_classification"  # the key of a run event that a plugin's classification, or its undoing, writes
CLASSIFIED = "classified"
RESTORED = "restored"

router = APIRouter(prefix="/v1/projects", tags=["runs"], responses={401: {"model": ErrorBody}})


class CauseRestored(BaseModel):
    run_id: int
    failure_cause: str | None = Field(description="the cause the run has now: the one the hub had guessed")
    was: str | None = Field(description="the cause the plugin had given it")


async def classified(conn, run_id: int):
    """The classification of run ``run_id`` still in force (the newest of its cause events, a ``classified`` one), or
    None."""
    e = tables.run_events
    step = e.c.body[CAUSE_KEY]["step"].astext
    newest = (
        await conn.execute(
            select(e.c.seq, e.c.body)
            .where(e.c.run_id == run_id, e.c.kind == "system", step.in_((CLASSIFIED, RESTORED)))
            .order_by(e.c.seq.desc())
            .limit(1)
        )
    ).one_or_none()
    if newest is None or newest.body[CAUSE_KEY]["step"] != CLASSIFIED:
        return None
    return newest


@router.post("/{project}/runs/{run_id}/cause/restore", response_model=CauseRestored)
async def restore_cause(request: Request, project: ProjectName, run_id: int, user: CurrentUser) -> CauseRestored:
    """Give a failed run back the cause it had before a core plugin classified it; the run's log says who did."""
    if not 1 <= run_id <= MAX_ID:
        raise HTTPException(422, "run_id is out of range")
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        if not has_role(access.role, "writer"):
            raise HTTPException(
                403, f"restoring the cause of a run of project {access.name} needs the writer role on it"
            )
        r = tables.runs
        row = (
            await conn.execute(
                select(r.c.failure_cause).where(r.c.id == run_id, r.c.project_id == access.project_id).with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise HTTPException(404, f"project {access.name} has no run {run_id}")
        found = await classified(conn, run_id)
        if found is None:
            raise HTTPException(409, f"no plugin's classification of the cause of run {run_id} is in force")
        note = found.body[CAUSE_KEY]
        previous = note["previous_cause"]
        await conn.execute(r.update().values(failure_cause=previous).where(r.c.id == run_id))
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
