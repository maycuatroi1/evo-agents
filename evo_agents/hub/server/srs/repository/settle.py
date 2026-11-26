"""The rows of a settle chat (``service.settle``): the version a review run reviewed, its settle chat, the open
findings of level decide, @ba's agent type, and the people the rules of the chat name."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.plugin_calls import enabled_in
from evo_agents.hub.server.srs import BA
from evo_agents.hub.server.srs.repository.findings import OPEN
from evo_agents.hub.server.srs.repository.store import PLUGIN
from evo_agents.hub.tables_srs import code_key


@dataclass(frozen=True)
class SettleVersion:
    """The version a settle chat is for, with its document and project."""

    id: int
    number: int
    state: str
    author_id: int
    review_run_id: int
    document_id: int
    slug: str
    title: str
    archived: bool
    project_id: int
    project: str


async def proposer(conn: AsyncConnection, run_id: int, document_id: int) -> int | None:
    """The author of the version whose settle chat chat run ``run_id`` is in, when that version is of document
    ``document_id``: who the version @ba proposes there is written as; None for a run of another chat."""
    r, sc, v = tables.runs, tables.srs.settle_chats, tables.srs.versions
    query = (
        select(v.c.author_id)
        .select_from(r.join(sc, sc.c.chat_id == r.c.chat_id).join(v, v.c.id == sc.c.version_id))
        .where(r.c.id == run_id, v.c.document_id == document_id)
    )
    return (await conn.execute(query)).scalar_one_or_none()


async def reviewed_version(conn: AsyncConnection, project_id: int, review_run_id: int) -> SettleVersion | None:
    """The version of the project whose current round's review is run ``review_run_id``, or None."""
    s, p = tables.srs, tables.projects
    v, d = s.versions, s.documents
    query = (
        select(
            v.c.id,
            v.c.number,
            v.c.state,
            v.c.author_id,
            v.c.review_run_id,
            d.c.id.label("document_id"),
            d.c.slug,
            d.c.title,
            d.c.archived_at.is_not(None).label("archived"),
            d.c.project_id,
            p.c.name.label("project"),
        )
        .select_from(v.join(d, d.c.id == v.c.document_id).join(p, p.c.id == d.c.project_id))
        .where(v.c.review_run_id == review_run_id, d.c.project_id == project_id)
    )
    row = (await conn.execute(query)).first()
    return None if row is None else SettleVersion(**row._mapping)


async def settle_chat(conn: AsyncConnection, version_id: int):
    """The settle chat of version ``version_id`` (its chat, first message and agent run), or None."""
    sc = tables.srs.settle_chats
    query = select(sc.c.chat_id, sc.c.root_message_id, sc.c.agent_run_id).where(sc.c.version_id == version_id)
    return (await conn.execute(query)).first()


async def lock_version(conn: AsyncConnection, version_id: int) -> None:
    """Hold the settling of version ``version_id`` for the transaction: one settle chat is opened for it."""
    await conn.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"srs-settle:{version_id}", 0))))


async def deciding(conn: AsyncConnection, document_id: int, level: str) -> list:
    """The open findings of ``level`` of a document, oldest first, with their items and the authors they keep."""
    s = tables.srs
    f, v, i = s.findings, s.versions, s.items
    query = (
        select(
            f.c.id,
            f.c.version_id,
            v.c.number.label("version"),
            f.c.item_id,
            i.c.key.label("item"),
            i.c.code.label("item_code"),
            i.c.kind.label("item_kind"),
            f.c.kind,
            f.c.summary,
            f.c.sources,
            f.c.commit_authors,
        )
        .select_from(f.join(v, v.c.id == f.c.version_id).outerjoin(i, i.c.id == f.c.item_id))
        .where(f.c.document_id == document_id, f.c.status == OPEN, f.c.level == level)
        .order_by(f.c.id)
    )
    return list(await conn.execute(query))


async def ba_type(conn: AsyncConnection, project_id: int):
    """@ba's agent type in the project (its profile id and run_as) while it is on, or None."""
    plugin = await enabled_in(conn, PLUGIN, project_id)
    if plugin is None:
        return None
    t, ap = tables.project_agent_types, tables.agent_profiles
    query = (
        select(t.c.profile_id, t.c.run_as)
        .join_from(t, ap, ap.c.id == t.c.profile_id)
        .where(t.c.project_id == project_id, t.c.plugin_id == plugin.id, t.c.name == BA)
        .where(t.c.disabled_at.is_(None), ap.c.deleted_at.is_(None))
    )
    return (await conn.execute(query)).first()


async def opened_on(conn: AsyncConnection, finding_ids: list[int], link: str, values: dict) -> None:
    """Keep the settle chat ``values`` names, and its ``link`` on each finding of ``finding_ids``."""
    f = tables.srs.findings
    await conn.execute(update(f).where(f.c.id.in_(finding_ids)).values(chat_link=link))
    await conn.execute(insert(tables.srs.settle_chats).values(values))


async def login(conn: AsyncConnection, user_id: int) -> str | None:
    u = tables.users
    return (await conn.execute(select(u.c.login).where(u.c.id == user_id))).scalar_one_or_none()


async def user_by_login(conn: AsyncConnection, name: str):
    """The user (id and login) whose login is ``name``, any case; or None."""
    u = tables.users
    return (await conn.execute(select(u.c.id, u.c.login).where(func.lower(u.c.login) == name.lower()))).first()


async def insert_chat(conn: AsyncConnection, values: dict) -> int:
    c = tables.chats
    return (await conn.execute(insert(c).values(values).returning(c.c.id))).scalar_one()


async def plan_author(conn: AsyncConnection, project_id: int, plan_id: str) -> str | None:
    """The member who wrote plan ``plan_id`` of the project on the hub: the author of its first revision."""
    pr, u = tables.plan_revisions, tables.users
    query = (
        select(u.c.login)
        .join_from(pr, u, u.c.id == pr.c.actor_id)
        .where(pr.c.project_id == project_id, pr.c.plan_id == plan_id)
        .order_by(pr.c.revision)
        .limit(1)
    )
    return (await conn.execute(query)).scalar_one_or_none()


async def holder(conn: AsyncConnection, project_id: int, code: str):
    """The item code ``code`` names in the project, with the latest version that holds it and its author; or None."""
    s, u = tables.srs, tables.users
    c, i, iv, v, d = s.codes, s.items, s.item_versions, s.versions, s.documents
    query = (
        select(c.c.item_id, v.c.number, d.c.slug, u.c.login)
        .select_from(
            c.join(i, i.c.id == c.c.item_id)
            .join(iv, iv.c.item_id == i.c.id)
            .join(v, v.c.id == iv.c.version_id)
            .join(d, d.c.id == v.c.document_id)
            .join(u, u.c.id == v.c.author_id)
        )
        .where(c.c.project_id == project_id, c.c.code_key == code_key(code))
        .order_by(v.c.number.desc())
        .limit(1)
    )
    return (await conn.execute(query)).first()


async def use_cases_above(conn: AsyncConnection, deciding: list) -> set[int]:
    """The use cases the business rules of ``deciding`` sit under, in the version each was found on."""
    s = tables.srs
    iv, i = s.item_versions, s.items
    found = set()
    for row in deciding:
        if row.item_id is None or row.item_kind == "use_case":
            continue
        query = (
            select(i.c.id)
            .join_from(iv, i, i.c.id == iv.c.parent_item_id)
            .where(iv.c.version_id == row.version_id, iv.c.item_id == row.item_id, i.c.kind == "use_case")
        )
        found |= set((await conn.execute(query)).scalars())
    return found


async def assignees(conn: AsyncConnection, items: set[int]) -> list[tuple[str, str]]:
    """(login, the code or key of the item) of the assignees of the tasks linked to ``items``."""
    if not items:
        return []
    t, u, i = tables.tasks, tables.users, tables.srs.items
    query = (
        select(u.c.login, func.coalesce(i.c.code, i.c.key))
        .select_from(
            t.links.join(t.people, t.people.c.task_id == t.links.c.task_id)
            .join(u, u.c.id == t.people.c.user_id)
            .join(i, i.c.id == t.links.c.srs_item_id)
        )
        .where(t.links.c.srs_item_id.in_(sorted(items)), t.people.c.role == "assignee")
        .order_by(u.c.login)
    )
    return [(row[0], row[1]) for row in await conn.execute(query)]


__all__ = [
    "SettleVersion",
    "assignees",
    "ba_type",
    "deciding",
    "holder",
    "insert_chat",
    "lock_version",
    "login",
    "opened_on",
    "plan_author",
    "proposer",
    "reviewed_version",
    "settle_chat",
    "use_cases_above",
    "user_by_login",
]
