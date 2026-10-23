"""Who answers for a run, and the card of its autonomy (plan autonomy-levels, criterion a12).

The person a decision goes to (``asked_of``), stored on the decision when it is asked (decisions.asked_of, schema 0115):

- a run whose principal is an intern: the person their primary seat reports to (``org.mentor``); once the decision has
  been open ESCALATE_AFTER, the leads of the intern's unit too (``escalate``, run by the hourly job hub.expire_seats),
  each told once and each then allowed to answer it;
- a run the hub made on its own (``run_autonomy.HUB_KINDS``): the sponsor of its agent profile's live seat;
- any other run: its principal; a run without one (a local run, one queued before schema 0113) its owner, as before.

So an intern never answers a decision of their own run (403): the decisions of a merge, a deploy or any other class
their autonomy holds are their mentor's. A notice of a class in notify goes to the principal, and to the person they
report to when they are an intern (``told_to``).

The card (``card``) is what the claim puts first in the prompt of a run with an agent (``autonomy_card.card``): the
run's snapshot as it is at the claim, with its principal's role, unit and mentor.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import autonomy_card, org, tables
from evo_agents.hub.server import run_autonomy

ESCALATE_AFTER = timedelta(hours=24)  # an intern's decision open this long goes to the leads of their unit too
ESCALATE_BATCH = 500


async def _run(conn: AsyncConnection, run_id: int):
    r = tables.runs
    columns = (r.c.kind, r.c.principal_id, r.c.dispatched_by, r.c.profile_id, r.c.autonomy)
    return (await conn.execute(select(*columns).where(r.c.id == run_id))).one()


async def asked_of(conn: AsyncConnection, run_id: int) -> int:
    """The person a decision of run ``run_id`` is asked of now (see the module)."""
    row = await _run(conn, run_id)
    if row.principal_id is None:
        return row.dispatched_by
    at = await run_autonomy.context(conn)
    if row.kind in run_autonomy.HUB_KINDS:
        sponsor = run_autonomy.sponsor(at.chart, row.profile_id, at.day)
        if sponsor is not None:
            return sponsor
    return org.mentor(at.chart, row.principal_id, at.day) or row.principal_id


async def told_to(conn: AsyncConnection, principal_id: int) -> list[int]:
    """Who a notice of a class in notify goes to: the principal, and the person an intern reports to."""
    at = await run_autonomy.context(conn)
    mentor = org.mentor(at.chart, principal_id, at.day)
    return [principal_id] if mentor is None else [principal_id, mentor]


async def escalated_to(conn: AsyncConnection, decision_id: int) -> list[int]:
    """The leads who may answer decision ``decision_id`` beside the person it is asked of: those of the unit of an
    intern principal, once it has been open ESCALATE_AFTER; none otherwise."""
    d, r = tables.decisions, tables.runs
    query = (
        select(r.c.principal_id, (d.c.asked_at < func.now() - ESCALATE_AFTER).label("late"))
        .join_from(d, r, r.c.id == d.c.run_id)
        .where(d.c.id == decision_id, d.c.asked_of.is_not(None))
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None or row.principal_id is None or not row.late:
        return []
    at = await run_autonomy.context(conn)
    if org.mentor(at.chart, row.principal_id, at.day) is None:
        return []
    return org.unit_leads(at.chart, row.principal_id, at.day)


async def _login(conn: AsyncConnection, user_id: int) -> str:
    u = tables.users
    return (await conn.execute(select(u.c.login).where(u.c.id == user_id))).scalar_one()


async def card(conn: AsyncConnection, run_id: int) -> autonomy_card.Card | None:
    """The autonomy card of run ``run_id`` from its snapshot now; None for a run without one."""
    row = await _run(conn, run_id)
    if row.autonomy is None or row.principal_id is None:
        return None
    at = await run_autonomy.context(conn)
    role, unit = org.placement(at.chart, row.principal_id, at.day)
    mentor = org.mentor(at.chart, row.principal_id, at.day)
    who = autonomy_card.Who(
        login=await _login(conn, row.principal_id),
        role=role,
        unit=unit,
        reports_to=None if mentor is None else await _login(conn, mentor),
    )
    return autonomy_card.card(row.autonomy, who)


async def escalate(engine: AsyncEngine) -> int:
    """Tell the leads of an intern's unit of each decision of the intern's runs open ESCALATE_AFTER, once each (a
    notification of the decision to them is the mark); how many were told."""
    # notifications reaches the run service, which builds a claim's card from this module
    from evo_agents.hub.server.notifications import decision_link, notify, one_line

    d, r, n = tables.decisions, tables.runs, tables.notifications
    told = 0
    async with engine.begin() as conn:
        late = (
            select(d.c.id, d.c.project_id, d.c.run_id, d.c.question, d.c.category, d.c.asked_of, r.c.principal_id)
            .join_from(d, r, r.c.id == d.c.run_id)
            .where(
                d.c.state == "open",
                d.c.asked_of.is_not(None),
                r.c.principal_id.is_not(None),
                d.c.asked_of != r.c.principal_id,
                d.c.asked_at < func.now() - ESCALATE_AFTER,
            )
            .order_by(d.c.id)
            .limit(ESCALATE_BATCH)
        )
        rows = (await conn.execute(late)).all()
        if not rows:
            return 0
        at = await run_autonomy.context(conn)
        for row in rows:
            if org.mentor(at.chart, row.principal_id, at.day) is None:
                continue
            intern, mentor = await _login(conn, row.principal_id), await _login(conn, row.asked_of)
            for lead in org.unit_leads(at.chart, row.principal_id, at.day):
                seen = exists().where(and_(n.c.decision_id == row.id, n.c.user_id == lead))
                if lead == row.asked_of or (await conn.execute(select(seen))).scalar_one():
                    continue
                await notify(
                    conn,
                    user_id=lead,
                    kind="decision",
                    project_id=row.project_id,
                    run_id=row.run_id,
                    decision_id=row.id,
                    title=one_line(f"Run #{row.run_id} of {intern} still asks: {row.question}"),
                    body=f"{row.question.strip()}\n\n{row.category} decision of a run of {intern}, an intern, asked "
                    f"of {mentor} more than 24 hours ago and not answered yet: you lead their unit, so you may answer "
                    "it too.",
                    link=decision_link(row.id),
                )
                told += 1
    return told
