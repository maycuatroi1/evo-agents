"""The plugin crm's reads and writes of its own tables (SQLAlchemy Core), and the refusal every module raises.

A contact is one row of crm_contacts for each login in a project; a touchpoint is one row for each kind and reference
of a contact, so a thing read twice is stored once. Deleting a contact deletes its touchpoints and its stage history
(the keys cascade) and leaves one row of crm_deletions that holds no login.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.crm import rules
from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.tables_crm import STAGES

PLUGIN = "crm"
SINK_HEADER = "X-Evo-Sink"  # evo_agents.hub.server.plans.SINK_HEADER
MAX_LIST = 500


class CrmError(Exception):
    """Why the plugin refuses a request, with the HTTP status a route answers; nothing was written."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Contact:
    id: int
    login: str
    profile_url: str
    source: str
    stage: str
    label: dict
    first_seen_at: datetime
    stage_at: datetime


# Who may do what


def reader(access: ProjectAccess) -> None:
    if access.role is None:
        raise CrmError(403, f"reading the contacts of project {access.name} needs a grant on it")


def writer(access: ProjectAccess, what: str) -> None:
    reader(access)
    if not has_role(access.role, "writer"):
        raise CrmError(403, f"{what} in project {access.name} needs the writer role; you are {access.role}")


def admin(access: ProjectAccess, what: str) -> None:
    reader(access)
    if not has_role(access.role, "admin"):
        raise CrmError(403, f"{what} in project {access.name} needs the admin role; you are {access.role}")


def visible(access: ProjectAccess, label, sink: str | None) -> bool:
    """Whether the caller reads a row with ``label`` through ``sink`` (the project's hub sink when none is named)."""
    through = sink or (access.rules.hub_sink.sink if access.rules.hub_sink else None)
    return through is not None and access.visible(label, through)


# Contacts


def _contact(row) -> Contact:
    return Contact(
        row.id, row.login, row.profile_url, row.source, row.stage, row.label, row.first_seen_at, row.stage_at
    )


def _columns():
    c = tables.crm_contacts.c
    return (c.id, c.login, c.profile_url, c.source, c.stage, c.label, c.first_seen_at, c.stage_at)


async def contacts(
    conn: AsyncConnection,
    project_id: int,
    *,
    stage: str | None = None,
    source: str | None = None,
    limit: int | None = MAX_LIST,
) -> list[Contact]:
    """The contacts of a project, newest first, optionally of one stage or source; ``limit`` None for all."""
    c = tables.crm_contacts.c
    query = select(*_columns()).where(c.project_id == project_id).order_by(c.first_seen_at.desc(), c.id.desc())
    if stage:
        query = query.where(c.stage == stage)
    if source:
        query = query.where(c.source == source)
    return [_contact(row) for row in await conn.execute(query if limit is None else query.limit(limit))]


async def contact(conn: AsyncConnection, project_id: int, login: str, *, lock: bool = False) -> Contact | None:
    c = tables.crm_contacts.c
    query = select(*_columns()).where(c.project_id == project_id, c.login == login.lower())
    row = (await conn.execute(query.with_for_update() if lock else query)).first()
    return _contact(row) if row else None


async def counts(conn: AsyncConnection, project_id: int) -> dict[str, int]:
    """stage -> how many contacts are in it now; every stage is there, 0 when empty."""
    c = tables.crm_contacts.c
    found = await conn.execute(select(c.stage, func.count()).where(c.project_id == project_id).group_by(c.stage))
    held = {stage: n for stage, n in found}
    return {stage: held.get(stage, 0) for stage in STAGES}


async def ensure_contact(
    conn: AsyncConnection, project_id: int, login: str, source: str, label: dict, seen_at: datetime
) -> tuple[int, bool]:
    """(the id of the contact with ``login``, whether this call made it). The source is the first one it was seen from."""
    t = tables.crm_contacts
    statement = (
        pg_insert(t)
        .values(
            project_id=project_id,
            login=login,
            profile_url=rules.profile_url(login),
            source=source,
            label=label,
            first_seen_at=seen_at,
            stage_at=seen_at,
        )
        .on_conflict_do_nothing(constraint="crm_contacts_login_key")
        .returning(t.c.id)
    )
    made = (await conn.execute(statement)).scalar()
    if made is not None:
        return made, True
    found = await conn.scalar(select(t.c.id).where(t.c.project_id == project_id, t.c.login == login))
    return found, False


async def add_touchpoint(
    conn: AsyncConnection,
    contact_id: int,
    kind: str,
    ref: str,
    happened_at: datetime,
    *,
    url: str | None = None,
    note: str | None = None,
    author_id: int | None = None,
) -> bool:
    """Whether the touchpoint was new; the same kind and reference twice is stored once."""
    t = tables.crm_touchpoints
    statement = (
        pg_insert(t)
        .values(
            contact_id=contact_id, kind=kind, ref=ref, url=url, note=note, author_id=author_id, happened_at=happened_at
        )
        .on_conflict_do_nothing(constraint="crm_touchpoints_once_key")
        .returning(t.c.id)
    )
    return (await conn.execute(statement)).scalar() is not None


async def touchpoints(conn: AsyncConnection, contact_id: int) -> list:
    t, u = tables.crm_touchpoints, tables.users
    query = (
        select(t.c.kind, t.c.ref, t.c.url, t.c.note, t.c.happened_at, u.c.login.label("author"))
        .join_from(t, u, u.c.id == t.c.author_id, isouter=True)
        .where(t.c.contact_id == contact_id)
        .order_by(t.c.happened_at.desc(), t.c.id.desc())
    )
    return list(await conn.execute(query))


async def history(conn: AsyncConnection, contact_id: int) -> list:
    h = tables.crm_stage_history.c
    found = await conn.execute(
        select(h.stage, h.reached_at).where(h.contact_id == contact_id).order_by(h.reached_at, h.id)
    )
    return sorted(found, key=lambda row: STAGES.index(row.stage))


async def record_stages(conn: AsyncConnection, contact_id: int, stored: str, times: dict[str, datetime]) -> str:
    """Keep the stages ``times`` holds in the history and move the contact to the highest, never down; the stage."""
    h = tables.crm_stage_history
    await conn.execute(
        pg_insert(h)
        .values([{"contact_id": contact_id, "stage": stage, "reached_at": at} for stage, at in times.items()])
        .on_conflict_do_nothing(constraint="crm_stage_history_once_key")
    )
    stage = rules.advance(stored, times)
    if stage != stored:
        c = tables.crm_contacts.c
        await conn.execute(
            update(tables.crm_contacts)
            .where(c.id == contact_id, c.stage == stored)
            .values(stage=stage, stage_at=times.get(stage, func.now()))
        )
    return stage


async def delete_contact(
    conn: AsyncConnection, project_id: int, login: str, deleted_by: int | None
) -> tuple[int, int] | None:
    """Delete the contact with ``login`` and everything kept about it: its touchpoints and stage history. Leaves a row
    of crm_deletions with its number and the count of touchpoints, no login. (its number, touchpoints deleted), None when there is no such contact."""
    found = await contact(conn, project_id, login, lock=True)
    if found is None:
        return None
    t, sent = tables.crm_touchpoints, tables.crm_invites_sent
    held = await conn.scalar(select(func.count()).where(t.c.contact_id == found.id))
    await conn.execute(delete(sent).where(sent.c.project_id == project_id, sent.c.login == found.login))
    await conn.execute(delete(tables.crm_contacts).where(tables.crm_contacts.c.id == found.id))
    await conn.execute(
        insert(tables.crm_deletions).values(
            project_id=project_id, contact_id=found.id, touchpoints=held, deleted_by=deleted_by
        )
    )
    return found.id, held


# Invite links @community made


async def requested(conn: AsyncConnection, project_id: int, login: str, ref: str) -> bool:
    """Whether contact ``login`` asked for an invite with the request ``ref`` (an invite_request touchpoint)."""
    c, t = tables.crm_contacts.c, tables.crm_touchpoints.c
    found = await conn.scalar(
        select(t.id)
        .join_from(tables.crm_touchpoints, tables.crm_contacts, c.id == t.contact_id)
        .where(c.project_id == project_id, c.login == login.lower(), t.kind == "invite_request", t.ref == ref)
        .limit(1)
    )
    return found is not None


async def sent_for(conn: AsyncConnection, project_id: int, ref: str) -> bool:
    s = tables.crm_invites_sent.c
    return await conn.scalar(select(s.id).where(s.project_id == project_id, s.ref == ref)) is not None


async def sent_this_week(conn: AsyncConnection, project_id: int) -> int:
    """How many invite links @community made in the project in the last 7 days."""
    s = tables.crm_invites_sent.c
    since = func.now() - timedelta(days=7)
    return int(await conn.scalar(select(func.count()).where(s.project_id == project_id, s.sent_at > since)) or 0)


async def add_sent(conn: AsyncConnection, project_id: int, ref: str, login: str, invite_id: int, run_id: int) -> None:
    await conn.execute(
        insert(tables.crm_invites_sent).values(
            project_id=project_id, ref=ref, login=login.lower(), invite_id=invite_id, run_id=run_id
        )
    )


# Cursors


async def cursor(conn: AsyncConnection, project_id: int, source: str) -> datetime | None:
    t = tables.crm_cursors
    return await conn.scalar(select(t.c.since).where(t.c.project_id == project_id, t.c.source == source))


async def move_cursor(conn: AsyncConnection, project_id: int, source: str, since: datetime) -> None:
    t = tables.crm_cursors
    statement = pg_insert(t).values(project_id=project_id, source=source, since=since)
    await conn.execute(
        statement.on_conflict_do_update(
            constraint="crm_cursors_pkey",
            set_={"since": func.greatest(t.c.since, statement.excluded.since), "updated_at": func.now()},
        )
    )
