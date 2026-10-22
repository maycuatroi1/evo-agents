"""What the worker tells the hub of a run's autonomy (plan autonomy-levels, criteria a8 and a9): the backups its agent
made, and the commands of a class in notify its PreToolUse hook let through (``evo_agents.worker.autonomy_hook``).

- ``POST /v1/worker/runs/{id}/backups``: ``evo-agents worker backup`` records a backup the agent made (what, where,
  when, how to restore it): a ``system`` event of the run and the audit row ``autonomy.backup``. A backup made in the
  future is refused (422); one made before ``BACKUP_FRESH`` ago is recorded, and does not open the floor S2 of K9.
- ``POST /v1/worker/runs/{id}/autonomy-acts``: the hook is about to let a command of ``klass`` through (a class in
  notify, or one in ask whose decision the agent names); the hub answers the class's mode now, the stricter of the
  run's snapshot and the value now (``run_autonomy.recheck``), and whether the decision is one of the run, of a
  category of the class, answered; it tells the run's principal (and the person an intern reports to) when ``klass``
  is notify for them now (``autonomy_gates.tell_after``, audited ``autonomy.notified``).

Both take the token of the worker that claimed the run, of any kind.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from evo_agents.hub import tables
from evo_agents.hub.autonomy_card import CLASS_CATEGORIES
from evo_agents.hub.autonomy_levels import NOTIFY
from evo_agents.hub.server import audit, autonomy_gates, run_autonomy
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.run_events import claimed_run
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.security import CurrentUser

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
REFUSALS = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}
BACKUP_FRESH = timedelta(hours=24)  # the floor S2: a backup made within this long before a deletion
SKEW = timedelta(minutes=5)  # how far in the future a backup's time may be, the clocks apart


class BackupIn(BaseModel):
    target: str = Field(min_length=1, max_length=500, description="what was backed up: a database, a table, a bucket")
    location: str = Field(
        min_length=1, max_length=1000, description="where the backup is: a path, a URL, a snapshot id"
    )
    made_at: datetime | None = Field(default=None, description="when it was made, with its time zone; now when absent")
    restore: str | None = Field(default=None, max_length=2000, description="the command that restores it")


class BackupOut(BaseModel):
    seq: int = Field(description="the run event that records the backup")
    made_at: datetime
    fresh: bool = Field(description="made within the last 24 hours: it opens the floor S2 of K9")


class AutonomyActIn(BaseModel):
    klass: Literal["K5", "K6", "K8", "K9", "K10", "K12"] = Field(description="the class of the command")
    what: str = Field(min_length=1, max_length=500, description="the command, cut, never a credential's value")
    decision_id: int | None = Field(
        default=None,
        ge=1,
        description="for a class in ask: the decision of the run, of the class's category, that the agent asked first",
    )


class AutonomyActOut(BaseModel):
    mode: Literal["deny", "ask", "notify", "auto"] | None = Field(
        description="the class's mode for the run's principal now, the stricter of its snapshot and the value now; "
        "null for a run without a snapshot"
    )
    told: bool = Field(description="whether the hub told the principal (mode notify)")
    answered: bool = Field(description="whether decision_id is a decision of the run, of the class, answered")


async def _project_id(conn, run_id: int) -> int:
    r = tables.runs
    return (await conn.execute(select(r.c.project_id).where(r.c.id == run_id))).scalar_one()


@worker_router.post("/runs/{run_id}/backups", response_model=BackupOut, status_code=201, responses=REFUSALS)
async def record_backup(request: Request, run_id: RunId, backup: BackupIn, user: CurrentUser) -> BackupOut:
    """Record a backup the run's agent made, as the module says."""
    now = datetime.now(UTC)
    made_at = backup.made_at or now
    if made_at.tzinfo is None:
        raise HTTPException(422, "made_at needs its time zone")
    if made_at > now + SKEW:
        raise HTTPException(422, f"made_at {made_at.isoformat()} is in the future")
    fresh = now - made_at <= BACKUP_FRESH
    async with request.app.state.engine.begin() as conn:
        await claimed_run(conn, user, run_id)
        body: dict = {"target": backup.target, "location": backup.location, "made_at": made_at.isoformat()}
        if backup.restore:
            body["restore"] = backup.restore
        text = f"Backup of {backup.target} at {backup.location}, made {made_at.isoformat()}"
        seq = await write_event(conn, run_id, {"text": text, "backup": body})
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.AUTONOMY_BACKUP,
            target=f"run:{run_id} {backup.target[:200]} at={backup.location[:300]} made={made_at.isoformat()}",
            project_id=await _project_id(conn, run_id),
        )
    return BackupOut(seq=seq, made_at=made_at, fresh=fresh)


async def _answered(conn, run_id: int, klass: str, decision_id: int | None) -> bool:
    if decision_id is None:
        return False
    d = tables.decisions
    query = select(d.c.state).where(
        d.c.id == decision_id, d.c.run_id == run_id, d.c.category.in_(CLASS_CATEGORIES.get(klass, ()))
    )
    return (await conn.execute(query)).scalar_one_or_none() == "answered"


@worker_router.post("/runs/{run_id}/autonomy-acts", response_model=AutonomyActOut, responses=REFUSALS)
async def autonomy_act(request: Request, run_id: RunId, act: AutonomyActIn, user: CurrentUser) -> AutonomyActOut:
    """The mode of the class of a command the run's agent is about to run, and whether its decision is answered; the
    principal told when the mode is notify, as the module says."""
    async with request.app.state.engine.begin() as conn:
        await claimed_run(conn, user, run_id)
        answered = await _answered(conn, run_id, act.klass, act.decision_id)
        found = await run_autonomy.recheck(conn, run_id)
        if found is None:
            return AutonomyActOut(mode=None, told=False, answered=answered)
        mode = autonomy_gates.mode_of(found.classes[act.klass], act.klass, False)
        if mode == NOTIFY:
            title = f"{act.klass}: the agent of run {run_id} ran {act.what}"
            await autonomy_gates.tell_after(conn, run_id, act.klass, key=f"hook {act.what[:200]}", title=title)
    return AutonomyActOut(mode=mode, told=mode == NOTIFY, answered=answered)
