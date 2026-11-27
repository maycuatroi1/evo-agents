"""What the worker tells the hub of a run's autonomy (plan autonomy-levels, criteria a8 and a9): the backups its agent
made, and the commands of a class in notify its PreToolUse hook let through (``evo_agents.worker.autonomy_hook``).

- ``POST /v1/worker/runs/{id}/backups``: ``evo-agents worker backup`` records a backup the agent made (what, where,
  when, how to restore it): a ``system`` event of the run and the audit row ``autonomy.backup``. A backup made in the
  future is refused (422); one made before ``BACKUP_FRESH`` ago is recorded, and does not open the floor S2 of K9.
- ``POST /v1/worker/runs/{id}/autonomy-acts``: the hook is about to let a command of ``klass`` through (a class in
  notify, or one in ask whose decision the agent names); the hub answers the class's mode now, the stricter of the
  run's snapshot and the value now (``run_autonomy.recheck``), and whether the decision is one of the run, of a
  category of the class, answered, and, for a merge approval bound to a head and a scope (``approval_heads``), given
  for the head and scope the hook read of the request now; one given for another says why (``stale``), and the agent
  asks again at the new head. It tells the run's principal (and the person an intern reports to) when ``klass``
  is notify for them now (``autonomy_gates.tell_after``, audited ``autonomy.notified``). The decision goes to the
  ledger S1 (``autonomy_ledger``).

Both take the token of the worker that claimed the run, of any kind.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from evo_agents.hub.server.autonomy.models.worker import AutonomyActIn, AutonomyActOut, BackupIn, BackupOut
from evo_agents.hub.server.autonomy.service import worker as service
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.run_events import claimed_run
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.security import CurrentUser

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
REFUSALS: dict[int | str, dict[str, Any]] = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}


@worker_router.post("/runs/{run_id}/backups", response_model=BackupOut, status_code=201, responses=REFUSALS)
async def record_backup(request: Request, run_id: RunId, backup: BackupIn, user: CurrentUser) -> BackupOut:
    """Record a backup the run's agent made, as the module says."""
    made_at, fresh = service.made_at(backup)
    async with request.app.state.engine.begin() as conn:
        await claimed_run(conn, user, run_id)
        seq = await service.record_backup(conn, user, run_id, backup, made_at)
    return BackupOut(seq=seq, made_at=made_at, fresh=fresh)


@worker_router.post("/runs/{run_id}/autonomy-acts", response_model=AutonomyActOut, responses=REFUSALS)
async def autonomy_act(request: Request, run_id: RunId, act: AutonomyActIn, user: CurrentUser) -> AutonomyActOut:
    """The mode of the class of a command the run's agent is about to run, and whether its decision is answered; the
    principal told when the mode is notify, as the module says."""
    async with request.app.state.engine.begin() as conn:
        await claimed_run(conn, user, run_id)
        return await service.act(conn, run_id, act)
