"""The members' channels of core plugins as the hub keeps them, in SQLAlchemy Core on ``evo_agents.hub.tables``: the
member a channel of a kind speaks for, found by its config; linking and unlinking a channel; the one-time codes a
channel is linked with (channel_link_codes, schema 0063); what a member may answer from a channel; what a channel's
class reads of a notification; and the state a channel keeps. The API of core plugins
(``evo_agents.hub.server.core_plugin_api``) builds on it, and ``docs/plugins.md`` (The core plugin API) is the design.

A channel is a row of notification_channels: one member, one kind, the config its plugin stores (a chat's ids, say),
and the web session that linked it (``token_id``). It speaks for its member only while that session lives: the same
member's web session, neither revoked nor expired (``live_session``). Linking takes a live session and replaces the
member's channel of that kind; ``unique_on`` names the config keys that identify a chat, so a chat speaks for one
member and another member's link to it goes. Unlinking deletes the channel, whose deliveries go with it.

These rules came from the Telegram channel (``evo_agents.core_plugins.telegram``), which reaches them through the API
like any other core plugin.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, bindparam, delete, exists, func, insert, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import access, tables, telegram
from evo_agents.hub.server import audit
from evo_agents.hub.server.notifications import SESSION_ENDED, Outgoing
from evo_agents.hub.server.security import WEB

MAX_CONFIG_BYTES = 16 * 1024  # a channel's config, or its state, as JSON
RESERVED_KEYS = ("disabled_reason", "disabled_at", "state")  # what the hub, not a link, writes into a channel's config
STATE = "state"
NOT_YOURS = "the hub has nothing by that for you to answer: it is not of a project you hold a grant on, or not there"
RESTRICTED_ANSWER = "answer this on the hub: its project keeps its decisions and proposals off this channel"
FROM_SESSION = (
    "a channel is linked from a web session only, so a token that leaked cannot link one of its own: sign in on the "
    "web and link it there"
)
NO_USER = "the hub has no user {login}"
ANSWERABLE = ("decision", "proposal")


@dataclass(frozen=True)
class Found:
    """A member's channel: who it speaks for, its id and its config as stored."""

    user_id: int
    login: str
    channel_id: int
    config: dict


def live_session(row_of):
    """The web session ``row_of`` (notification_channels or channel_link_codes) is bound to is live: of the same
    member, neither revoked nor expired."""
    t = tables.tokens
    return exists().where(
        t.c.id == row_of.c.token_id,
        t.c.user_id == row_of.c.user_id,
        t.c.kind == WEB,
        t.c.revoked_at.is_(None),
        t.c.expires_at > func.now(),
    )


async def session_live(conn: AsyncConnection, user_id: int, token_id: int | None) -> bool:
    """Whether ``token_id`` is a live web session of member ``user_id`` now."""
    if token_id is None:
        return False
    t = tables.tokens
    query = select(t.c.id).where(
        t.c.id == token_id,
        t.c.user_id == user_id,
        t.c.kind == WEB,
        t.c.revoked_at.is_(None),
        t.c.expires_at > func.now(),
    )
    return (await conn.execute(query)).first() is not None


def json_object(value, what: str, limit: int = MAX_CONFIG_BYTES) -> dict:
    """``value`` as a JSON object of at most ``limit`` bytes; ValueError naming ``what`` otherwise."""
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{what} is a JSON object with string keys")
    try:
        text = json.dumps(dict(value), separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what} is not JSON: {exc}") from None
    if len(text.encode()) > limit:
        raise ValueError(f"{what} is at most {limit // 1024} KiB of JSON")
    return json.loads(text)


def _found(row) -> Found:
    return Found(row.user_id, row.login, row.id, dict(row.config or {}))


def _channels():
    c, u = tables.notification_channels, tables.users
    return select(c.c.id, c.c.user_id, u.c.login, c.c.config).join_from(c, u, u.c.id == c.c.user_id)


async def member_by_config(
    conn: AsyncConnection, kind: str, match: Mapping[str, Any], *, live: bool = True
) -> Found | None:
    """The channel of ``kind`` whose config holds every key and value of ``match``; with ``live``, only while the web
    session that linked it lives, without it whatever became of that session (to unlink it)."""
    c = tables.notification_channels
    query = _channels().where(c.c.kind == kind, c.c.config.contains(dict(match)))
    if live:
        query = query.where(live_session(c))
    row = (await conn.execute(query.order_by(c.c.id).limit(1))).one_or_none()
    return None if row is None else _found(row)


async def linked(conn: AsyncConnection, user_id: int, kind: str):
    """Member ``user_id``'s channel of ``kind`` as it stands (config, enabled, created_at, and session_live: the web
    session that linked it lives), or None."""
    c = tables.notification_channels
    live = live_session(c).label("session_live")
    query = select(c.c.config, c.c.enabled, c.c.created_at, live).where(c.c.user_id == user_id, c.c.kind == kind)
    return (await conn.execute(query)).one_or_none()


async def user_named(conn: AsyncConnection, login: str):
    """The user (id, login) of ``login`` in any case; 404 when the hub has none."""
    u = tables.users
    query = select(u.c.id, u.c.login).where(func.lower(u.c.login) == func.lower(login))
    found = (await conn.execute(query)).one_or_none()
    if found is None:
        raise HTTPException(404, NO_USER.format(login=login))
    return found


async def member_channel(conn: AsyncConnection, kind: str, user_id: int, *, live: bool = True) -> Found | None:
    """Member ``user_id``'s channel of ``kind``, while the web session that linked it lives with ``live``."""
    c = tables.notification_channels
    query = _channels().where(c.c.kind == kind, c.c.user_id == user_id)
    if live:
        query = query.where(live_session(c))
    row = (await conn.execute(query)).one_or_none()
    return None if row is None else _found(row)


async def link(
    conn: AsyncConnection,
    *,
    user_id: int,
    login: str,
    token_id: int,
    kind: str,
    config: dict,
    unique_on: tuple[str, ...] = (),
) -> Found:
    """Link member ``user_id``'s channel of ``kind`` with ``config``, bound to web session ``token_id``, in place of
    the one they had; another member's channel of the kind whose config has the same values at ``unique_on`` goes.
    403 SESSION_ENDED when the session does not live now. Audited as ``<kind>.link``."""
    if not await session_live(conn, user_id, token_id):
        raise HTTPException(403, SESSION_ENDED)
    c = tables.notification_channels
    if unique_on:
        same = {key: config[key] for key in unique_on}
        await conn.execute(delete(c).where(c.c.kind == kind, c.c.user_id != user_id, c.c.config.contains(same)))
    bound = {"config": config, "enabled": True, "token_id": token_id}
    linked = pg_insert(c).values(user_id=user_id, kind=kind, **bound)
    linked = linked.on_conflict_do_update(
        constraint="notification_channels_user_id_kind_key", set_={**bound, "created_at": func.now()}
    ).returning(c.c.id)
    channel_id = (await conn.execute(linked)).scalar_one()
    await audit.record(conn, actor_id=user_id, token_id=token_id, action=f"{kind}.link", target=f"{kind} user:{login}")
    return Found(user_id, login, channel_id, dict(config))


async def unlink(
    conn: AsyncConnection,
    *,
    user_id: int,
    kind: str,
    by: str,
    actor_id: int | None = None,
    actor_token_id: int | None = None,
) -> bool:
    """Delete member ``user_id``'s channel of ``kind`` and the deliveries waiting for it; whether there was one.
    Audited as ``<kind>.unlink`` done ``by`` the member (web or bot), or by ``actor_id``, a hub admin (admin)."""
    c, u = tables.notification_channels, tables.users
    gone = (await conn.execute(delete(c).where(c.c.user_id == user_id, c.c.kind == kind).returning(c.c.id))).all()
    if gone:
        login = (await conn.execute(select(u.c.login).where(u.c.id == user_id))).scalar_one()
        await audit.record(
            conn,
            actor_id=user_id if actor_id is None else actor_id,
            token_id=actor_token_id,
            action=f"{kind}.unlink",
            target=f"{kind} user:{login} by={by}",
        )
    return bool(gone)


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


async def new_link_code(conn: AsyncConnection, *, user_id: int, token_id: int, kind: str) -> tuple[str, datetime]:
    """A one-time code that links member ``user_id``'s channel of ``kind``, made by web session ``token_id``, and when
    it expires: it lives ``telegram.LINK_SECONDS``, the hub keeps its SHA-256 alone, and it replaces the member's codes
    of the kind not used yet. 403 SESSION_ENDED when the session does not live now."""
    if not await session_live(conn, user_id, token_id):
        raise HTTPException(403, SESSION_ENDED)
    codes = tables.channel_link_codes
    day_ago = func.now() - timedelta(days=1)
    mine = and_(codes.c.user_id == user_id, codes.c.kind == kind)
    await conn.execute(
        delete(codes).where(
            mine | (codes.c.expires_at < day_ago), codes.c.used_at.is_(None) | (codes.c.used_at < day_ago)
        )
    )
    code = telegram.new_code()
    made = (
        insert(codes)
        .values(
            user_id=user_id,
            kind=kind,
            code_hash=telegram.code_hash(code),
            expires_at=func.now() + timedelta(seconds=telegram.LINK_SECONDS),
            token_id=token_id,
        )
        .returning(codes.c.expires_at)
    )
    return code, (await conn.execute(made)).scalar_one()


async def redeem_link_code(conn: AsyncConnection, kind: str, code: str):
    """The member (user_id, login, token_id) code ``code`` of ``kind`` was made for, now used; None when it is not one
    of the kind, was used, expired, or the web session that made it does not live any more."""
    if not isinstance(code, str) or not telegram.LINK_CODE.fullmatch(code):
        return None
    codes, users = tables.channel_link_codes, tables.users
    query = (
        select(codes.c.id, codes.c.user_id, codes.c.token_id, users.c.login)
        .join_from(codes, users, users.c.id == codes.c.user_id)
        .where(
            codes.c.code_hash == telegram.code_hash(code),
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


def kept_from(row, level: str, location: str | None, below: bool = False) -> bool:
    """Whether the project of ``row`` (of ``project_rules``) is kept from a channel cleared for level and location,
    or for what sits below them (``telegram.restricted_for``); no project is."""
    if row is None:
        return True
    return telegram.restricted_for(row.clearance, row.levels, row.locations, level, location, below=below)


async def rules_of(conn: AsyncConnection, project: str):
    """The row of ``project_rules`` of project ``project``, or None."""
    return (await conn.execute(project_rules().where(tables.projects.c.name == project))).one_or_none()


async def answerable_project(
    conn: AsyncConnection,
    user_id: int,
    kind: str,
    item_id: int,
    level: str,
    location: str | None,
    below: bool = False,
) -> str:
    """The name of the project of decision or proposal ``item_id``, once member ``user_id`` may answer it from a
    channel cleared for ``level`` and ``location``: 404 NOT_YOURS without a grant on it, the same as for nothing by
    that id, and 403 RESTRICTED_ANSWER for a project kept from the channel. Checked before anything of the decision
    or proposal is read."""
    item = tables.decisions if kind == "decision" else tables.proposals
    p, g, ps = tables.projects, tables.grants, tables.project_sinks
    query = (
        select(p.c.name, p.c.levels, p.c.locations, ps.c.clearance)
        .select_from(
            item.join(p, p.c.id == item.c.project_id)
            .join(g, and_(g.c.project_id == p.c.id, g.c.user_id == user_id))
            .outerjoin(ps, and_(ps.c.project_id == p.c.id, ps.c.kind == access.HUB_KIND))
        )
        .where(item.c.id == item_id)
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        raise HTTPException(404, NOT_YOURS)
    if kept_from(row, level, location, below):
        raise HTTPException(403, RESTRICTED_ANSWER)
    return row.name


async def prepare(
    conn: AsyncConnection, notification: Outgoing, level: str, location: str | None, below: bool = False
) -> Outgoing:
    """``notification`` with what a channel cleared for ``level`` and ``location`` may send of it in ``extra``: only
    ``{"restricted": true}`` when its project is kept from the channel (or it is a decision or proposal of no project),
    else its decision (question, category, plan_id, step_key, options, state, run_kind, run_title) and its proposal
    (title, tier, kind, lens, evidence_count, state), as Telegram's channel reads them."""
    restricted = notification.kind in ANSWERABLE
    if notification.project is not None:
        restricted = kept_from(await rules_of(conn, notification.project), level, location, below)
    if restricted:
        return dataclasses.replace(notification, extra={"restricted": True})
    extra: dict = {"restricted": False}
    if notification.decision_id is not None:
        d, r = tables.decisions, tables.runs
        query = select(
            d.c.question,
            d.c.category,
            d.c.plan_id,
            d.c.step_key,
            d.c.options,
            d.c.state,
            r.c.kind.label("run_kind"),
            r.c.title.label("run_title"),
        ).join_from(d, r, r.c.id == d.c.run_id)
        row = (await conn.execute(query.where(d.c.id == notification.decision_id))).one_or_none()
        extra["decision"] = None if row is None else dict(row._mapping)
    if notification.proposal_id is not None:
        p = tables.proposals
        query = select(p.c.title, p.c.tier, p.c.kind, p.c.lens, p.c.evidence_count, p.c.state)
        row = (await conn.execute(query.where(p.c.id == notification.proposal_id))).one_or_none()
        extra["proposal"] = None if row is None else dict(row._mapping)
    return dataclasses.replace(notification, extra=extra)


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


__all__ = [
    "FROM_SESSION",
    "MAX_CONFIG_BYTES",
    "NOT_YOURS",
    "NO_USER",
    "RESERVED_KEYS",
    "RESTRICTED_ANSWER",
    "Found",
    "answerable_project",
    "delivered",
    "json_object",
    "kept_from",
    "link",
    "linked",
    "live_session",
    "member_by_config",
    "member_channel",
    "new_link_code",
    "prepare",
    "project_rules",
    "redeem_link_code",
    "rules_of",
    "session_live",
    "set_state",
    "unlink",
    "user_named",
]
