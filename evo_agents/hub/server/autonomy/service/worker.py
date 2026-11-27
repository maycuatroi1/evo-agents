"""What the worker tells the hub of a run's autonomy, as ``autonomy.routes.worker`` says: the backup its agent made,
and the act its hook lets through."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import approval_heads
from evo_agents.hub.autonomy_card import CLASS_CATEGORIES
from evo_agents.hub.autonomy_levels import ACTING, ASK, NOTIFY
from evo_agents.hub.server import audit
from evo_agents.hub.server.autonomy.models.worker import BACKUP_FRESH, SKEW, AutonomyActIn, AutonomyActOut, BackupIn
from evo_agents.hub.server.autonomy.repository import worker as store
from evo_agents.hub.server.autonomy.service import gates as autonomy_gates
from evo_agents.hub.server.autonomy.service import ledger as autonomy_ledger
from evo_agents.hub.server.autonomy.service import runs as run_autonomy
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Unprocessable
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.security import Principal


def made_at(backup: BackupIn) -> tuple[datetime, bool]:
    """When the backup was made, and whether it is fresh; 422 without a time zone or in the future."""
    now = datetime.now(UTC)
    made = backup.made_at or now
    if made.tzinfo is None:
        raise Unprocessable("made_at needs its time zone")
    if made > now + SKEW:
        raise Unprocessable(f"made_at {made.isoformat()} is in the future")
    return made, now - made <= BACKUP_FRESH


async def record_backup(
    conn: AsyncConnection, user: Principal, run_id: int, backup: BackupIn, made_at: datetime
) -> int:
    """Write the run event and the audit row of the backup; the event's seq."""
    body: dict = {"target": backup.target, "location": backup.location, "made_at": made_at.isoformat()}
    if backup.restore:
        body["restore"] = backup.restore
    text = f"Backup of {backup.target} at {backup.location}, made {made_at.isoformat()}"
    seq = await write_event(conn, run_id, {"text": text, "backup": body})
    target = f"run:{run_id} {backup.target[:200]} at={backup.location[:300]} made={made_at.isoformat()}"
    await Auditor(conn, await store.project_id(conn, run_id), user).record(audit.AUTONOMY_BACKUP, target)
    return seq


async def _answered(conn: AsyncConnection, run_id: int, act: AutonomyActIn) -> tuple[bool, str | None]:
    """Whether the decision the act names is the run's, of the class, answered, and holds for the act's head and
    scope; and why not when its approval was given for another."""
    if act.decision_id is None:
        return False, None
    found = await store.decision(conn, act.decision_id, run_id, CLASS_CATEGORIES.get(act.klass, ()))
    if found is None or found.state != "answered":
        return False, None
    why = approval_heads.stale(found.approval, act.head, act.scope)
    return not why, why or None


async def act(conn: AsyncConnection, run_id: int, act: AutonomyActIn) -> AutonomyActOut:
    """The mode of the act's class now and whether its decision is answered, written to the ledger; the principal
    told when the mode is notify."""
    answered, stale = await _answered(conn, run_id, act)
    found = await run_autonomy.recheck(conn, run_id)
    if found is None:
        return AutonomyActOut(mode=None, told=False, answered=answered, stale=stale)
    mode = autonomy_gates.mode_of(found.classes[act.klass], act.klass, False)
    passes = mode in ACTING or (mode == ASK and answered)
    result = autonomy_ledger.ALLOWED if passes else autonomy_ledger.REFUSED
    await autonomy_ledger.record(conn, run_id, act.klass, mode, result, f"hook {act.what[:200]}")
    if mode == NOTIFY:
        title = f"{act.klass}: the agent of run {run_id} ran {act.what}"
        await autonomy_gates.tell_after(conn, run_id, act.klass, key=f"hook {act.what[:200]}", title=title)
    return AutonomyActOut(mode=mode, told=mode == NOTIFY, answered=answered, stale=stale)  # pyright: ignore[reportArgumentType]
