"""The ledger S1 of autonomy (plan autonomy-levels, criterion a10): every decision a gate of the hub makes for a run
writes the audit row ``autonomy.decided`` naming the run, its principal, its agent, the class, the mode and the result,
whatever the result: the gates of K5, K6, K9 to K11 (``gates``), K7 at a lease (``lease``), K8 and K12 at the
routes (``guards``) and the acts the worker's hook reports (``autonomy.routes.worker``). A decision the
gate makes again unchanged, as it does while it waits for an answer, is written once. The worker writes the hook's own
decisions as run events, with the same fields (``evo_agents.worker.autonomy_hook``).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server import audit
from evo_agents.hub.server.autonomy.repository import ledger as store

ACTED, ASKED, REFUSED, ALLOWED = "acted", "asked", "refused", "allowed"
NONE = "-"  # a principal or an agent the run has none of


def target(run_id: int, principal: str | None, agent: str | None, klass: str, mode: str, result: str, act: str) -> str:
    """The audit target of one decision."""
    return f"run:{run_id} principal={principal or NONE} agent={agent or NONE} {klass} mode={mode} result={result} {act}"


async def record(
    conn: AsyncConnection,
    run_id: int,
    klass: str,
    mode: str | None,
    result: str,
    act: str,
    *,
    token_id: int | None = None,
) -> None:
    """Write the decision of the gate of ``klass`` on run ``run_id`` for ``act``, once; nothing for a run the hub no
    longer has."""
    row = await store.run_people(conn, run_id)
    if row is None:
        return
    text = target(run_id, row.login, row.handle, klass, mode or NONE, result, act)
    if await store.decided_before(conn, text):
        return
    await audit.record(
        conn,
        actor_id=row.principal_id,
        token_id=token_id,
        action=audit.AUTONOMY_DECIDED,
        target=text,
        project_id=row.project_id,
    )
