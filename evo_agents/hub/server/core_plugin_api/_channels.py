"""The members' channels of a core plugin through the API: finding the member a channel speaks for, linking and
unlinking it, where a member's link stands, one-time link codes, the state a channel keeps, and what a channel's class
sends of a notification. Private to ``core_plugin_api``; ``evo_agents.hub.server.member_channels`` holds the
queries."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, tables
from evo_agents.hub.server import member_channels
from evo_agents.hub.server.core_plugin_api._types import (
    Clearance,
    LinkCode,
    LinkedChannel,
    Member,
    NotificationRef,
    Plugin,
    Refused,
    Session,
    Unlinked,
    admin_session,
    issue_member,
    issue_session,
    own_clearance,
    own_kind,
    own_member,
    own_plugin,
    own_session,
    refusing,
)
from evo_agents.hub.server.member_channels import FROM_SESSION, MAX_CONFIG_BYTES, RESERVED_KEYS
from evo_agents.hub.server.notifications import SESSION_ENDED, Outgoing

MAX_MATCH_KEYS = 8
UNLINKED = "this channel is not linked any more: its member links it again"
TURNED_OFF = "turned off"  # the reason of a channel turned off without one


def _member(plugin: Plugin, kind: str, found: member_channels.Found) -> Member:
    return issue_member(plugin.id, kind, found.user_id, found.login, found.channel_id, found.config)


def _match(match) -> dict:
    """``match`` as a find of channels takes it: 1 to MAX_MATCH_KEYS keys, each a string or an integer."""
    if not isinstance(match, Mapping) or not 1 <= len(match) <= MAX_MATCH_KEYS:
        raise ValueError(f"a match names 1 to {MAX_MATCH_KEYS} keys of a channel's config")
    for key, value in match.items():
        if not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("a match maps keys of a channel's config to strings or integers")
    return dict(match)


async def find_member(
    conn: AsyncConnection, plugin: Plugin, kind: str, match: Mapping[str, str | int], *, live: bool = True
) -> Member | None:
    """The member whose channel of ``kind`` has every key and value of ``match`` in its config; with ``live`` (the
    default), only while the web session that linked the channel lives. ``live=False`` is for unlinking alone."""
    own_kind(own_plugin(plugin), kind)
    found = await member_channels.member_by_config(conn, kind, _match(match), live=live)
    return None if found is None else _member(plugin, kind, found)


async def link_channel(
    conn: AsyncConnection,
    plugin: Plugin,
    session: Session,
    kind: str,
    config: Mapping[str, Any],
    *,
    unique_on: tuple[str, ...] = (),
) -> Member:
    """Link the session's member's channel of ``kind`` with ``config``, in place of the one they had, bound to that
    web session, which it lives no longer than; another member's channel of the kind with the same values at
    ``unique_on`` goes, so a chat speaks for one member. 403 for a credential that is not a web session, or a
    session that ended. Audited as ``<kind>.link``."""
    own_kind(own_plugin(plugin), kind)
    own_session(session)
    stored = member_channels.json_object(config, "a channel's config", MAX_CONFIG_BYTES)
    reserved = sorted(set(stored) & set(RESERVED_KEYS))
    if reserved:
        raise ValueError(f"the hub writes {', '.join(reserved)} into a channel's config, not a link")
    unique = tuple(unique_on)
    if any(key not in stored for key in unique):
        raise ValueError("unique_on names keys of the channel's config")
    if not session.web:
        raise Refused(403, FROM_SESSION)
    with refusing():
        found = await member_channels.link(
            conn,
            user_id=session.user_id,
            login=session.login,
            token_id=session.token_id,
            kind=kind,
            config=stored,
            unique_on=unique,
        )
    return _member(plugin, kind, found)


async def unlink_channel(
    conn: AsyncConnection,
    plugin: Plugin,
    kind: str,
    user_id: int,
    *,
    by: Literal["web", "bot", "admin"],
    actor: Session | None = None,
) -> bool:
    """Unlink member ``user_id``'s channel of ``kind``: the channel and the deliveries waiting for it go; whether
    there was one. ``by`` the member on the web or in the channel, or by a hub admin, ``actor``, from ``HubAdmin``.
    Audited as ``<kind>.unlink``."""
    own_kind(own_plugin(plugin), kind)
    if by not in ("web", "bot", "admin"):
        raise ValueError("an unlink is by web, bot or admin")
    if by == "admin":
        admin_session(actor)
    elif actor is not None:
        raise ValueError("only an unlink by admin names an actor")
    return await member_channels.unlink(
        conn,
        user_id=user_id,
        kind=kind,
        by=by,
        actor_id=None if actor is None else actor.user_id,
        actor_token_id=None if actor is None else actor.token_id,
    )


async def unlink_by_login(conn: AsyncConnection, plugin: Plugin, kind: str, login: str, *, actor: Session) -> Unlinked:
    """A hub admin, ``actor`` from ``HubAdmin``, unlinks the channel of ``kind`` of the member named ``login`` (in any
    case), whatever its session, as ``unlink_channel`` by admin does: 404 when the hub has no such user."""
    own_kind(own_plugin(plugin), kind)
    admin_session(actor)
    if not isinstance(login, str):
        raise ValueError("a login is a string")
    with refusing():
        found = await member_channels.user_named(conn, login)
    gone = await member_channels.unlink(
        conn, user_id=found.id, kind=kind, by="admin", actor_id=actor.user_id, actor_token_id=actor.token_id
    )
    return Unlinked(found.login, gone)


async def linked_channel(conn: AsyncConnection, plugin: Plugin, session: Session, kind: str) -> LinkedChannel | None:
    """Where the session's member's channel of ``kind`` stands, for any credential of theirs: its config, when it was
    linked, whether the hub sends to it and why not (what turned it off, or SESSION_ENDED once the web session that
    linked it ended); None when there is none."""
    own_kind(own_plugin(plugin), kind)
    own_session(session)
    row = await member_channels.linked(conn, session.user_id, kind)
    if row is None:
        return None
    config = dict(row.config or {})
    reason = None
    if not row.enabled:
        reason = str(config.get("disabled_reason") or TURNED_OFF)
    elif not row.session_live:
        reason = SESSION_ENDED
    return LinkedChannel(MappingProxyType(config), row.created_at, reason is None, reason)


async def set_channel_state(conn: AsyncConnection, member: Member, state: Mapping[str, Any]) -> None:
    """Keep ``state`` (a JSON object of at most 16 KiB) as the ``state`` of the member's channel's config, in place of
    the one it had, its row locked: what the plugin remembers, such as the ids of the messages it sent. 404 when the
    channel was unlinked since."""
    own_member(member)
    kept = member_channels.json_object(state, "a channel's state", MAX_CONFIG_BYTES)
    if not await member_channels.set_state(conn, member.channel_id, member.user_id, kept):
        raise Refused(404, UNLINKED)


async def direct_chat_member(conn: AsyncConnection, plugin: Plugin, kind: str, chat_id: int) -> Member | None:
    """The member of direct chat ``chat_id`` when their channel of ``kind`` is linked and its web session lives; None
    for a group chat, or a member without one."""
    own_kind(own_plugin(plugin), kind)
    c = tables.chats
    query = select(c.c.direct_user_id).where(c.c.id == chat_id, c.c.kind == chats.DIRECT)
    user_id = (await conn.execute(query)).scalar_one_or_none()
    found = None if user_id is None else await member_channels.member_channel(conn, kind, user_id)
    return None if found is None else _member(plugin, kind, found)


async def delivered_notification(conn: AsyncConnection, member: Member, external_id: str) -> NotificationRef | None:
    """The member's own notification their channel delivered as ``external_id`` (what the class's ``send``
    answered); None for any other."""
    own_member(member)
    row = await member_channels.delivered(conn, member.user_id, member.kind, external_id)
    if row is None:
        return None
    return NotificationRef(row.id, row.kind, row.project, row.decision_id, row.proposal_id)


async def prepare(conn: AsyncConnection, notification: Outgoing, cleared: Clearance) -> Outgoing:
    """``notification`` with what a channel cleared for ``cleared`` may send of it in ``extra``: ``{"restricted":
    true}`` alone when its project is kept from the channel, or it is a decision or a proposal of no project; else its
    decision and its proposal, each None when gone, as ``docs/plugins.md`` lists their fields."""
    if not isinstance(notification, Outgoing):
        raise ValueError("prepare takes the Outgoing a channel's prepare is given")
    own_clearance(cleared)
    return await member_channels.prepare(conn, notification, cleared.level, cleared.location, cleared.below)


async def link_code(conn: AsyncConnection, plugin: Plugin, session: Session, kind: str) -> LinkCode:
    """A one-time code that links the session's member's channel of ``kind`` when the plugin redeems it: 32
    characters of base64url, ten minutes, the hub keeping its SHA-256 alone, bound to the web session, which must still
    live when it is redeemed; it replaces the member's codes of the kind not used yet. 403 for a credential that is not
    a web session, or one that ended."""
    own_kind(own_plugin(plugin), kind)
    own_session(session)
    if not session.web:
        raise Refused(403, FROM_SESSION)
    with refusing():
        code, expires_at = await member_channels.new_link_code(
            conn, user_id=session.user_id, token_id=session.token_id, kind=kind
        )
    return LinkCode(code, expires_at)


async def redeem_link_code(conn: AsyncConnection, plugin: Plugin, kind: str, code: str) -> Session | None:
    """The web session that made ``code``, a code of ``kind`` not used yet and not expired, whose session lives: the
    code is used now, and ``link_channel`` takes the session. None otherwise."""
    own_kind(own_plugin(plugin), kind)
    row = await member_channels.redeem_link_code(conn, kind, code)
    return None if row is None else issue_session(row.user_id, row.login, row.token_id, True)
