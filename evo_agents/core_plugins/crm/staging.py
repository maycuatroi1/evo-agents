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
from evo_agents.core_plugins.crm import tables as crm
from evo_agents.hub.server import core_plugin_api as hub

INVITING = ("invite_link", "grant")  # the touchpoints that make a contact invited


async def _seen(conn: AsyncConnection, project_id: int) -> dict[int, tuple[datetime, datetime | None]]:
    """contact id -> (its first touchpoint, its first invite or grant), notes left out."""
    c, t = crm.contacts, crm.touchpoints
    invited = func.min(t.c.happened_at).filter(t.c.kind.in_(INVITING))
    query = (
        select(t.c.contact_id, func.min(t.c.happened_at), invited)
        .join_from(t, c, c.c.id == t.c.contact_id)
        .where(c.c.project_id == project_id, t.c.kind != "note")
        .group_by(t.c.contact_id)
    )
    return {row[0]: (row[1], row[2]) for row in await conn.execute(query)}


async def _accounts(conn: AsyncConnection, logins: list[str]) -> dict[str, int]:
    """login, in lower case -> the id of the member of the hub with that login."""
    return {user.login.lower(): user.id for user in await hub.match_users(conn, store.handle(), logins)}


async def _runs(conn: AsyncConnection, project: str, user_ids: list[int]) -> dict[int, datetime]:
    """user id -> the first run the user dispatched in the project."""
    return dict(await hub.read_first_runs(conn, store.handle(), project, user_ids))


async def _retained(conn: AsyncConnection, project: str, user_id: int, first: datetime) -> datetime | None:
    start, end = rules.window(first)
    found = await hub.read_first_runs(conn, store.handle(), project, [user_id], since=start, before=end)
    return found.get(user_id)


async def facts(conn: AsyncConnection, project: hub.EnabledProject, logins: dict[int, str]) -> dict[int, rules.Facts]:
    """The facts of each contact in ``logins`` (id -> login)."""
    seen = await _seen(conn, project.id)
    accounts = await _accounts(conn, sorted(set(logins.values())))
    firsts = await _runs(conn, project.name, sorted(set(accounts.values())))
    found = {}
    for contact_id, login in logins.items():
        if contact_id not in seen:
            continue
        first_seen, invited = seen[contact_id]
        user_id = accounts.get(login)
        first_run = firsts.get(user_id) if user_id is not None else None
        retained = await _retained(conn, project.name, user_id, first_run) if first_run is not None else None
        found[contact_id] = rules.Facts(first_seen, invited, first_run, retained)
    return found


async def refresh(conn: AsyncConnection, project: hub.EnabledProject) -> dict[str, int]:
    """Compute and keep the stage of every contact of the project; how many moved up."""
    held = await store.contacts(conn, project.id, limit=None)
    by_id = {c.id: c for c in held}
    moved = 0
    for contact_id, found in (await facts(conn, project, {c.id: c.login for c in held})).items():
        stage = await store.record_stages(conn, contact_id, by_id[contact_id].stage, rules.reached(found))
        moved += stage != by_id[contact_id].stage
    return {"moved": moved}
