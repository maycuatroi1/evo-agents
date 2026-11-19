"""The plan that already delivers what a build of the Loop holds, so the build ends with a deliver of it.

A build of a group of the backlog holds its use cases in ``delivering`` from the tick that made it until its deliver
ends (``srs_ledger.hold``), so ``backlog.naming`` no longer holds that group for a plan written later. A member's author
run, or a person, can still write a plan for those use cases while the build waits in the queue; the build's own author
run then finds the plan and reuses it (the harness asks for that rather than a second plan of the same scope) and puts
none. Before this module, such a build waited for a person (``author_question``) or failed for want of a plan, its use
cases held in ``delivering`` with no deliver (hub M1, project m1-msp, work items 200, 201, 211 and 217, 2026-11-19).

``covering`` finds, for each build, the one plan that covers it: every use case the build holds in the ledger is named
in the plan as a whole word (``srs_backlog.names`` on ``plan_text``, non-goals left out, as ``backlog.naming`` reads
plans); the plan is active, or completed and neither abandoned nor superseded; and no work item of the Loop has it but
builds and a deliver that is not cancelled. When several plans cover a build, the one a deliver of the Loop has wins;
when that leaves more than one, none does, and a person adopts one (``loop adopt``). The rules (``rules.move_build``)
end a queued build covered by a plan before any author run is handed out for it, and a build whose author run ended
done with no plan of its own; ``author_mode.turn_ended`` ends a covered build's author run whose turn left no question.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.plans import PLAN_ABANDON_STATUSES
from evo_agents.hub.server.loop.model import BUILD, CANCELLED, DELIVER, Cover
from evo_agents.hub.srs_backlog import DELIVERING, names, plan_text

ACTIVE, COMPLETED = "active", "completed"  # plans.AREAS a covering plan may be in


async def covering(conn: AsyncConnection, project_id: int, build_ids: Iterable[int]) -> dict[int, Cover]:
    """The plan that covers each of builds ``build_ids`` of project ``project_id`` (see the module), by build id; a
    build that holds no use case in the ledger, or that no plan or more than one plan covers, is left out."""
    wanted = sorted(set(build_ids))
    if not wanted:
        return {}
    s = tables.srs_delivery
    query = select(s.c.item_id, s.c.code).where(
        s.c.project_id == project_id, s.c.state == DELIVERING, s.c.item_id.in_(wanted)
    )
    held: dict[int, set[str]] = defaultdict(set)
    for item_id, code in await conn.execute(query):
        if isinstance(code, str) and code.strip():
            held[item_id].add(code)
    if not held:
        return {}
    p, i = tables.plans, tables.loop_items
    words = sorted({code.split()[-1].casefold() for codes in held.values() for code in codes})
    text = func.lower(cast(p.c.body, Text), type_=Text)
    query = select(p.c.plan_id, p.c.area, p.c.body).where(
        p.c.project_id == project_id,
        p.c.area.in_((ACTIVE, COMPLETED)),
        or_(*(text.contains(word, autoescape=True) for word in words)),
    )
    plans = {}
    for plan_id, area, body in await conn.execute(query.order_by(p.c.plan_id)):
        if area == COMPLETED and isinstance(body, dict) and body.get("status") in PLAN_ABANDON_STATUSES:
            continue  # dropped, not delivered
        plans[plan_id] = (area == COMPLETED, plan_text(body))
    if not plans:
        return {}
    query = select(i.c.plan_id, i.c.kind, i.c.state).where(i.c.project_id == project_id, i.c.plan_id.in_(sorted(plans)))
    others: set[str] = set()  # plans a work item of the Loop has that is no build and no live deliver
    delivered: set[str] = set()  # plans a deliver of the Loop not cancelled has
    for plan_id, kind, state in await conn.execute(query):
        if kind == DELIVER and state != CANCELLED:
            delivered.add(plan_id)
        elif kind != BUILD:
            others.add(plan_id)
    found: dict[int, Cover] = {}
    for item_id, codes in held.items():
        covers = [
            plan_id
            for plan_id, (_, words_of_plan) in plans.items()
            if plan_id not in others and all(names(words_of_plan, code) for code in codes)
        ]
        live = [plan_id for plan_id in covers if plan_id in delivered]
        chosen = live if live else covers
        if len(chosen) == 1:
            found[item_id] = Cover(chosen[0], plans[chosen[0]][0])
    return found


__all__ = ["Cover", "covering"]
