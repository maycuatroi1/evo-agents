"""What the end of a review run does with its proposals (``evo_agents.hub.server.proposals``): the notice run_failed
when it failed, and its open tier 2 proposals as Inbox items of its owner (notifications of kind proposal), the most
evidence first, as long as the day has room under the charter's ``max_decisions_per_day``: the proposals that became
Inbox items on the same local day of the charter's time zone count against it; the others stay in the list alone.
``run_state`` calls ``review_ended`` in the transaction that ends the run.
"""

from __future__ import annotations

import logging

from sqlalchemy import Date, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.server.notifications import notify, run_link
from evo_agents.hub.server.run_state import RunStep

log = logging.getLogger(__name__)


async def _inbox_room(conn: AsyncConnection, project_id: int, charter: dict) -> int:
    """How many more tier 2 proposals of the project may become Inbox items today, in the charter's time zone."""
    p = tables.proposals
    zone = charter["window"]["timezone"]
    today = cast(func.timezone(zone, func.now()), Date)
    used = (
        await conn.execute(
            select(func.count()).where(
                p.c.project_id == project_id,
                p.c.inbox_at.is_not(None),
                cast(func.timezone(zone, p.c.inbox_at), Date) == today,
            )
        )
    ).scalar_one()
    return max(0, int(charter.get("max_decisions_per_day", 0)) - used)


async def review_ended(conn: AsyncConnection, found: RunStep, old: str, new: str, *, reason: str) -> list[int]:
    """What the end of review run ``found`` does: the notice run_failed when it failed, and its open tier 2
    proposals as Inbox items of its owner while the day has room (see the module's docstring); their ids."""
    if new not in runs.TERMINAL_STATES:
        return []
    project_id = found.project_id
    if new == "failed":
        error = found.error or reason
        await notify(
            conn,
            user_id=found.dispatcher_id,
            kind="notice",
            notice_kind="run_failed",
            project_id=project_id,
            run_id=found.run_id,
            title=f"Review run #{found.run_id} of {found.project} failed",
            body=error,
            details={"run_kind": "review", "error": error},
            link=run_link(found.project, found.run_id),
        )
    c, p = tables.charters, tables.proposals
    charter = (
        await conn.execute(select(c.c.body).where(c.c.project_id == project_id).order_by(c.c.revision.desc()).limit(1))
    ).scalar_one_or_none()
    if charter is None:
        return []
    room = await _inbox_room(conn, project_id, charter)
    if room <= 0:
        return []
    waiting = (
        await conn.execute(
            select(p.c.id, p.c.title, p.c.kind, p.c.lens, p.c.summary, p.c.evidence_count)
            .where(p.c.run_id == found.run_id, p.c.tier == 2, p.c.state == "open", p.c.inbox_at.is_(None))
            .order_by(p.c.evidence_count.desc(), p.c.id)
            .limit(room)
        )
    ).all()
    for row in waiting:
        await conn.execute(update(p).values(inbox_at=func.now()).where(p.c.id == row.id))
        await notify(
            conn,
            user_id=found.dispatcher_id,
            kind="proposal",
            project_id=project_id,
            run_id=found.run_id,
            proposal_id=row.id,
            title=f"Proposal #{row.id} (tier 2): {row.title}",
            body=row.summary,
            details={"tier": 2, "kind": row.kind, "lens": row.lens, "evidence": row.evidence_count},
            link=proposal_link(row.id),
        )
    if waiting:
        log.info("proposals in the inbox", extra={"run_id": found.run_id, "proposals": [row.id for row in waiting]})
    return [row.id for row in waiting]


def proposal_link(proposal_id: int) -> str:
    """Where the web shows a proposal and its answer buttons."""
    return f"/inbox?proposal={proposal_id}"
