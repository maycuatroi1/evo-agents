"""The stage of each contact of a project, computed from what the hub holds (``rules``) and kept with its history.

The facts: when a contact was first seen (its oldest touchpoint that is not a note), when an invite or a grant first
named its login, and, for a login that has an account on the hub, the first run it dispatched in the project and
whether it dispatched one from day 21 to day 28 after that. ``refresh`` computes the stage of every contact, adds each
stage it reached to ``crm_stage_history`` with the time it was reached, and moves the contact up, never down.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.crm import rules, store
from evo_agents.hub import tables

INVITING = ("invite_link", "grant")  # the touchpoints that make a contact invited


async def _seen(conn: AsyncConnection, project_id: int) -> dict[int, tuple[datetime, datetime | None]]:
    """contact id -> (its first touchpoint, its first invite or grant), notes left out."""
    c, t = tables.crm_contacts, tables.crm_touchpoints
    invited = func.min(t.c.happened_at).filter(t.c.kind.in_(INVITING))
    query = (
        select(t.c.contact_id, func.min(t.c.happened_at), invited)
        .join_from(t, c, c.c.id == t.c.contact_id)
        .where(c.c.project_id == project_id, t.c.kind != "note")
        .group_by(t.c.contact_id)
    )
    return {row[0]: (row[1], row[2]) for row in await conn.execute(query)}


async def _accounts(conn: AsyncConnection, logins: list[str]) -> dict[str, int]:
    u = tables.users
    found = await conn.execute(select(func.lower(u.c.login), u.c.id).where(func.lower(u.c.login).in_(logins)))
    return {login: user_id for login, user_id in found}


async def _runs(conn: AsyncConnection, project_id: int, user_ids: list[int]) -> dict[int, datetime]:
    """user id -> the first run the user dispatched in the project."""
    r = tables.runs.c
    query = (
        select(r.dispatched_by, func.min(r.queued_at))
        .where(r.project_id == project_id, r.dispatched_by.in_(user_ids))
        .group_by(r.dispatched_by)
    )
    return {user_id: first for user_id, first in await conn.execute(query)}


async def _retained(conn: AsyncConnection, project_id: int, user_id: int, first: datetime) -> datetime | None:
    r = tables.runs.c
    start, end = rules.window(first)
    return await conn.scalar(
        select(func.min(r.queued_at)).where(
            r.project_id == project_id, r.dispatched_by == user_id, r.queued_at >= start, r.queued_at < end
        )
    )


async def facts(conn: AsyncConnection, project_id: int, logins: dict[int, str]) -> dict[int, rules.Facts]:
    """The facts of each contact in ``logins`` (id -> login)."""
    seen = await _seen(conn, project_id)
    accounts = await _accounts(conn, sorted(set(logins.values())))
    firsts = await _runs(conn, project_id, sorted(set(accounts.values())))
    found = {}
    for contact_id, login in logins.items():
        if contact_id not in seen:
            continue
        first_seen, invited = seen[contact_id]
        user_id = accounts.get(login)
        first_run = firsts.get(user_id) if user_id is not None else None
        retained = await _retained(conn, project_id, user_id, first_run) if first_run is not None else None
        found[contact_id] = rules.Facts(first_seen, invited, first_run, retained)
    return found


async def refresh(conn: AsyncConnection, project_id: int) -> dict[str, int]:
    """Compute and keep the stage of every contact of the project; how many moved up."""
    held = await store.contacts(conn, project_id, limit=None)
    by_id = {c.id: c for c in held}
    moved = 0
    for contact_id, found in (await facts(conn, project_id, {c.id: c.login for c in held})).items():
        stage = await store.record_stages(conn, contact_id, by_id[contact_id].stage, rules.reached(found))
        moved += stage != by_id[contact_id].stage
    return {"moved": moved}
