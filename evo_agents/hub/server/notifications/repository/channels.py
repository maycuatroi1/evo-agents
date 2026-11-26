"""The members' channels in SQL: whether the web session a channel or a link code is bound to lives, a member's
channel found by its config or its kind, a channel bound (made or replaced) and deleted, the state it keeps, the
one-time link codes (channel_link_codes, schema 0063), the rules of a project and of a decision a channel is judged
by, what a channel reads of a decision and a proposal, and the notification a channel delivered as a message."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, bindparam, delete, exists, func, insert, or_, select, update
from sqlalchemy import null as sqlalchemy_null
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import access, tables
from evo_agents.hub.server.notifications.models import STATE, WEB_SESSION, Found


def live_session(row_of):
    """The web session ``row_of`` (notification_channels or channel_link_codes) is bound to is live: of the same
    member, neither revoked nor expired."""
    t = tables.tokens
    return exists().where(
        t.c.id == row_of.c.token_id,
        t.c.user_id == row_of.c.user_id,
        t.c.kind == WEB_SESSION,
        t.c.revoked_at.is_(None),
        t.c.expires_at > func.now(),
    )


def admin_linked(c):
    """The channel of notification_channels ``c`` was linked by a hub admin who is still a user: no session, an
    admin named (``admin_link``)."""
    return and_(c.c.token_id.is_(None), c.c.linked_by.is_not(None))


def channel_live(c):
    """The channel of notification_channels ``c`` speaks for its member: the web session that linked it lives, or a
    hub admin linked it."""
    return or_(live_session(c), admin_linked(c))


async def session_live(conn: AsyncConnection, user_id: int, token_id: int | None) -> bool:
    """Whether ``token_id`` is a live web session of member ``user_id`` now."""
    if token_id is None:
        return False
    t = tables.tokens
    query = select(t.c.id).where(
        t.c.id == token_id,
        t.c.user_id == user_id,
        t.c.kind == WEB_SESSION,
        t.c.revoked_at.is_(None),
        t.c.expires_at > func.now(),
    )
    return (await conn.execute(query)).first() is not None


def _found(row) -> Found:
    return Found(row.user_id, row.login, row.id, dict(row.config or {}))


def _channels():
    c, u = tables.notification_channels, tables.users
    return select(c.c.id, c.c.user_id, u.c.login, c.c.config).join_from(c, u, u.c.id == c.c.user_id)


async def member_by_config(
    conn: AsyncConnection, kind: str, match: Mapping[str, Any], *, live: bool = True
) -> Found | None:
    """The channel of ``kind`` whose config holds every key and value of ``match``; with ``live``, only while the web
    session that linked it lives or a hub admin linked it (``channel_live``), without it whatever became of that
    session (to unlink it)."""
    c = tables.notification_channels
    query = _channels().where(c.c.kind == kind, c.c.config.contains(dict(match)))
    if live:
        query = query.where(channel_live(c))
    row = (await conn.execute(query.order_by(c.c.id).limit(1))).one_or_none()
    return None if row is None else _found(row)


async def linked(conn: AsyncConnection, user_id: int, kind: str):
    """Member ``user_id``'s channel of ``kind`` as it stands (config, enabled, created_at, and session_live: the web
    session that linked it lives, or a hub admin linked it), or None."""
    c = tables.notification_channels
    live = channel_live(c).label("session_live")
    query = select(c.c.config, c.c.enabled, c.c.created_at, live).where(c.c.user_id == user_id, c.c.kind == kind)
    return (await conn.execute(query)).one_or_none()


async def user_named(conn: AsyncConnection, login: str):
    """The user (id, login) of ``login`` in any case, or None."""
    u = tables.users
    query = select(u.c.id, u.c.login).where(func.lower(u.c.login) == func.lower(login))
    return (await conn.execute(query)).one_or_none()


async def member_channel(conn: AsyncConnection, kind: str, user_id: int, *, live: bool = True) -> Found | None:
    """Member ``user_id``'s channel of ``kind``, with ``live`` while the web session that linked it lives or a hub
    admin linked it."""
    c = tables.notification_channels
    query = _channels().where(c.c.kind == kind, c.c.user_id == user_id)
    if live:
        query = query.where(channel_live(c))
    row = (await conn.execute(query)).one_or_none()
    return None if row is None else _found(row)


async def bind(conn: AsyncConnection, user_id: int, kind: str, bound: dict, unique_on: tuple[str, ...]) -> int:
    """Member ``user_id``'s channel of ``kind`` made, or replaced, with the columns ``bound``; another member's channel
    of the kind whose config has the same values at ``unique_on`` goes. The channel's id."""
    c = tables.notification_channels
    if unique_on:
        same = {key: bound["config"][key] for key in unique_on}
        await conn.execute(delete(c).where(c.c.kind == kind, c.c.user_id != user_id, c.c.config.contains(same)))
    linked_ = pg_insert(c).values(user_id=user_id, kind=kind, **bound)
    linked_ = linked_.on_conflict_do_update(
        constraint="notification_channels_user_id_kind_key", set_={**bound, "created_at": func.now()}
    ).returning(c.c.id)
    return (await conn.execute(linked_)).scalar_one()


async def delete_channel(conn: AsyncConnection, user_id: int, kind: str) -> str | None:
    """Delete member ``user_id``'s channel of ``kind`` and the deliveries waiting for it; the member's login when there
    was one, else None."""
    c, u = tables.notification_channels, tables.users
    gone = (await conn.execute(delete(c).where(c.c.user_id == user_id, c.c.kind == kind).returning(c.c.id))).all()
    if not gone:
        return None
    return (await conn.execute(select(u.c.login).where(u.c.id == user_id))).scalar_one()


async def set_state(conn: AsyncConnection, channel_id: int, user_id: int, state: dict) -> bool:
    """Put ``state`` in the config of channel ``channel_id`` of member ``user_id``, its row locked; False when the
    channel is gone."""
    c = tables.notification_channels
    locked = select(c.c.id).where(c.c.id == channel_id, c.c.user_id == user_id).with_for_update()
    if (await conn.execute(locked)).first() is None:
        return False
    kept = bindparam("state", {STATE: state}, type_=JSONB)
    await conn.execute(update(c).values(config=c.c.config.op("||")(kept)).where(c.c.id == channel_id))
    return True


# The one-time codes


async def new_link_code(
    conn: AsyncConnection, *, user_id: int, token_id: int, kind: str, code_hash: str, seconds: int
) -> datetime:
    """Store a link code of ``kind`` for member ``user_id``, by its ``code_hash``, made by web session ``token_id``
    and living ``seconds``, in place of the member's codes of the kind not used yet (and of any code a day stale);
    when it expires."""
    codes = tables.channel_link_codes
    day_ago = func.now() - timedelta(days=1)
    mine = and_(codes.c.user_id == user_id, codes.c.kind == kind)
    await conn.execute(
        delete(codes).where(
            mine | (codes.c.expires_at < day_ago), codes.c.used_at.is_(None) | (codes.c.used_at < day_ago)
        )
    )
    made = (
        insert(codes)
        .values(
            user_id=user_id,
            kind=kind,
            code_hash=code_hash,
            expires_at=func.now() + timedelta(seconds=seconds),
            token_id=token_id,
        )
        .returning(codes.c.expires_at)
    )
    return (await conn.execute(made)).scalar_one()


async def redeem_link_code(conn: AsyncConnection, kind: str, code_hash: str):
    """The member (user_id, login, token_id) the code of ``kind`` hashed ``code_hash`` was made for, now used; None
    when it is not one of the kind, was used, expired, or the web session that made it does not live any more."""
    codes, users = tables.channel_link_codes, tables.users
    query = (
        select(codes.c.id, codes.c.user_id, codes.c.token_id, users.c.login)
        .join_from(codes, users, users.c.id == codes.c.user_id)
        .where(
            codes.c.code_hash == code_hash,
            codes.c.kind == kind,
            codes.c.used_at.is_(None),
            codes.c.expires_at > func.now(),
            live_session(codes),
        )
        .with_for_update(of=codes)
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        return None
    await conn.execute(update(codes).values(used_at=func.now()).where(codes.c.id == row.id))
    return row


# What a channel may answer and read


def project_rules():
    """A project's name, ladder and hub sink's clearance (NULL without one), as ``telegram.restricted_for`` reads
    them."""
    p, ps = tables.projects, tables.project_sinks
    hub_sink = and_(ps.c.project_id == p.c.id, ps.c.kind == access.HUB_KIND)
    return select(p.c.name, p.c.levels, p.c.locations, ps.c.clearance).select_from(p.outerjoin(ps, hub_sink))


async def rules_of(conn: AsyncConnection, project: str):
    """The row of ``project_rules`` of project ``project``, or None."""
    return (await conn.execute(project_rules().where(tables.projects.c.name == project))).one_or_none()


def channel_sink(project_id, channel: str | None):
    """The clearance of the sink of kind channel named ``channel`` in project ``project_id`` (a column or a value), as
    a scalar subquery: NULL without one, or without a channel."""
    ps = tables.project_sinks.alias("channel_sink")
    return (
        select(ps.c.clearance)
        .where(ps.c.project_id == project_id, ps.c.kind == access.CHANNEL_KIND, ps.c.sink_id == (channel or ""))
        .limit(1)
        .scalar_subquery()
    )


async def answer_rules(conn: AsyncConnection, user_id: int, kind: str, item_id: int, channel: str | None):
    """The project's name, ladder and hub sink's clearance, the label and the clearance of the project's sink of
    channel ``channel`` of decision or proposal ``item_id``, when member ``user_id`` holds a grant on its project;
    None otherwise."""
    item = tables.decisions if kind == "decision" else tables.proposals
    p, g, ps = tables.projects, tables.grants, tables.project_sinks
    label = item.c.label if kind == "decision" else sqlalchemy_null()
    query = (
        select(
            p.c.name,
            p.c.levels,
            p.c.locations,
            ps.c.clearance,
            label.label("label"),
            channel_sink(p.c.id, channel).label("channel_clearance"),
        )
        .select_from(
            item.join(p, p.c.id == item.c.project_id)
            .join(g, and_(g.c.project_id == p.c.id, g.c.user_id == user_id))
            .outerjoin(ps, and_(ps.c.project_id == p.c.id, ps.c.kind == access.HUB_KIND))
        )
        .where(item.c.id == item_id)
    )
    return (await conn.execute(query)).one_or_none()


async def decision_rules(conn: AsyncConnection, decision_id: int, channel: str | None):
    """Decision ``decision_id``'s label, its project's name, ladder, default label and hub sink's clearance, and the
    clearance of the project's sink of channel ``channel``; None without the decision."""
    d, p, ps = tables.decisions, tables.projects, tables.project_sinks
    hub_sink = and_(ps.c.project_id == p.c.id, ps.c.kind == access.HUB_KIND)
    query = (
        select(
            d.c.label,
            d.c.state,
            p.c.name,
            p.c.levels,
            p.c.locations,
            p.c.default_label,
            ps.c.clearance,
            channel_sink(p.c.id, channel).label("channel_clearance"),
        )
        .select_from(d.join(p, p.c.id == d.c.project_id).outerjoin(ps, hub_sink))
        .where(d.c.id == decision_id)
    )
    return (await conn.execute(query)).one_or_none()


async def channel_decision(conn: AsyncConnection, decision_id: int):
    """What a channel reads of decision ``decision_id``, its texts as stored; None without it."""
    d, r, u = tables.decisions, tables.runs, tables.users
    query = select(
        d.c.question,
        d.c.category,
        d.c.plan_id,
        d.c.step_key,
        d.c.options,
        d.c.state,
        r.c.kind.label("run_kind"),
        r.c.title.label("run_title"),
        d.c.recommended_reason,
        d.c.reversible,
        d.c.undo,
        d.c.evidence,
        d.c.context,
        d.c.answer_option,
        u.c.login.label("answered_by"),
        d.c.answered_via,
    ).select_from(d.join(r, r.c.id == d.c.run_id).outerjoin(u, u.c.id == d.c.answered_by))
    return (await conn.execute(query.where(d.c.id == decision_id))).one_or_none()


async def channel_proposal(conn: AsyncConnection, proposal_id: int) -> dict | None:
    """What a channel reads of proposal ``proposal_id``; None without it."""
    p = tables.proposals
    query = select(p.c.title, p.c.tier, p.c.kind, p.c.lens, p.c.evidence_count, p.c.state)
    row = (await conn.execute(query.where(p.c.id == proposal_id))).one_or_none()
    return None if row is None else dict(row._mapping)


async def delivered(conn: AsyncConnection, user_id: int, kind: str, external_id: str):
    """The notification (id, kind, project, decision_id, proposal_id) a channel of ``kind`` of member ``user_id``
    delivered as ``external_id``, when it is the member's own; None otherwise."""
    n, dl, c, p = tables.notifications, tables.notification_deliveries, tables.notification_channels, tables.projects
    query = (
        select(n.c.id, n.c.kind, p.c.name.label("project"), n.c.decision_id, n.c.proposal_id)
        .select_from(
            dl.join(n, n.c.id == dl.c.notification_id)
            .join(c, c.c.id == dl.c.channel_id)
            .outerjoin(p, p.c.id == n.c.project_id)
        )
        .where(c.c.kind == kind, c.c.user_id == user_id, dl.c.external_id == external_id, n.c.user_id == user_id)
        .order_by(dl.c.id.desc())
        .limit(1)
    )
    return (await conn.execute(query)).one_or_none()
