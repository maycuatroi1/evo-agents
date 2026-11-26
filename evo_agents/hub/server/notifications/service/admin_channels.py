"""Members' channels a hub admin links and unlinks, for a member who does not reach the hub's web: PUT
/v1/admin/users/{login}/channels/{kind} links the member's channel of a kind a core plugin serves (a chat of theirs)
with the config its plugin keeps, bound to no session of theirs, so it speaks for the member until it is unlinked or
linked again, their own link from the web among them (``member_channels.admin_link``). DELETE unlinks a member's
channel of a kind, whoever linked it and whatever its session. Both are K12 writes for a run's credential
(``route_autonomy``) and audited as ``<kind>.link`` and ``<kind>.unlink`` with ``by=admin``.
"""

from __future__ import annotations

from fastapi import HTTPException

from evo_agents.hub.plugin_calls import core_registry
from evo_agents.hub.server.notifications.models import (
    RESERVED_KEYS,
    ChannelLink,
    ChannelLinked,
    ChannelUnlinked,
)
from evo_agents.hub.server.notifications.service import member_channels
from evo_agents.hub.server.security import Principal


def _served_kind(kind: str) -> None:
    """404 unless a core plugin of this hub contributes channels of ``kind``: the hub's own (web, push) take no link."""
    if not any(item["name"] == kind for _, item in core_registry().contributions("channels")):
        raise HTTPException(404, f"no core plugin of this hub serves channels of kind {kind}")


def _link_config(body: ChannelLink) -> tuple[dict, tuple[str, ...]]:
    """The config and the unique keys of ``body`` as a link stores them; 422 for what a link may not hold."""
    try:
        config = member_channels.json_object(body.config, "a channel's config")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    reserved = sorted(set(config) & set(RESERVED_KEYS))
    if reserved:
        raise HTTPException(422, f"the hub writes {', '.join(reserved)} into a channel's config, not a link")
    if not config:
        raise HTTPException(422, "a channel's config names the member's chat: it is not empty")
    unique = tuple(dict.fromkeys(body.unique_on))
    if any(key not in config for key in unique):
        raise HTTPException(422, "unique_on names keys of the channel's config")
    return config, unique


async def link(engine, user: Principal, login: str, kind: str, body: ChannelLink) -> ChannelLinked:
    """Link the channel of ``kind`` of member ``login`` with ``body``'s config, as hub admin ``user``."""
    _served_kind(kind)
    config, unique = _link_config(body)
    async with engine.begin() as conn:
        found = await member_channels.admin_link(
            conn,
            login=login,
            kind=kind,
            config=config,
            unique_on=unique,
            actor_id=user.user_id,
            actor_token_id=user.token_id,
        )
    return ChannelLinked(login=found.login, kind=kind, config=found.config, linked_by=user.login)


async def unlink(engine, user: Principal, login: str, kind: str) -> ChannelUnlinked:
    """Unlink the channel of ``kind`` of member ``login``, as hub admin ``user``."""
    _served_kind(kind)
    async with engine.begin() as conn:
        found = await member_channels.user_named(conn, login)
        gone = await member_channels.unlink(
            conn, user_id=found.id, kind=kind, by="admin", actor_id=user.user_id, actor_token_id=user.token_id
        )
    return ChannelUnlinked(login=found.login, kind=kind, unlinked=gone)
