"""Members' channels a hub admin links and unlinks, for a member who does not reach the hub's web: PUT
/v1/admin/users/{login}/channels/{kind} links the member's channel of a kind a core plugin serves (a chat of theirs)
with the config its plugin keeps, bound to no session of theirs, so it speaks for the member until it is unlinked or
linked again, their own link from the web among them (``member_channels.admin_link``). DELETE unlinks a member's
channel of a kind, whoever linked it and whatever its session. Both are K12 writes for a run's credential
(``route_autonomy``) and audited as ``<kind>.link`` and ``<kind>.unlink`` with ``by=admin``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, Field

from evo_agents.hub.plugin_calls import core_registry
from evo_agents.hub.server import member_channels, route_autonomy
from evo_agents.hub.server.admin import MISSING, Login
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import AdminUser, admin
from evo_agents.plugins.manifest import CHANNEL_KIND

router = APIRouter(
    prefix="/v1/admin", tags=["admin"], dependencies=[Depends(admin)], responses={401: {"model": ErrorBody}}
)

ChannelKind = Annotated[str, Path(pattern=CHANNEL_KIND)]


class ChannelLink(BaseModel):
    config: dict[str, Any] = Field(
        description="the channel's config as its plugin keeps it (the ids of the member's chat), at most 16 KiB of JSON"
    )
    unique_on: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="keys of config that name one chat: another member's channel of the kind with the same values "
        "there goes, so a chat speaks for one member",
    )


class ChannelLinked(BaseModel):
    login: str
    kind: str
    config: dict[str, Any]
    linked_by: str = Field(description="the hub admin who linked it")


class ChannelUnlinked(BaseModel):
    login: str
    kind: str
    unlinked: bool = Field(description="whether the member had a channel of the kind")


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
    reserved = sorted(set(config) & set(member_channels.RESERVED_KEYS))
    if reserved:
        raise HTTPException(422, f"the hub writes {', '.join(reserved)} into a channel's config, not a link")
    if not config:
        raise HTTPException(422, "a channel's config names the member's chat: it is not empty")
    unique = tuple(dict.fromkeys(body.unique_on))
    if any(key not in config for key in unique):
        raise HTTPException(422, "unique_on names keys of the channel's config")
    return config, unique


@router.put(
    "/users/{login}/channels/{kind}",
    response_model=ChannelLinked,
    responses={**MISSING, 422: {"model": ErrorBody}},
    dependencies=[route_autonomy.K12],
)
async def link_channel(
    request: Request, body: ChannelLink, login: Login, kind: ChannelKind, user: AdminUser
) -> ChannelLinked:
    """Link the channel of ``kind`` of member ``login`` with ``config``, as a hub admin, bound to no session of theirs,
    in place of the one they had."""
    _served_kind(kind)
    config, unique = _link_config(body)
    async with request.app.state.engine.begin() as conn:
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


@router.delete(
    "/users/{login}/channels/{kind}",
    response_model=ChannelUnlinked,
    responses={**MISSING},
    dependencies=[route_autonomy.K12],
)
async def unlink_channel(request: Request, login: Login, kind: ChannelKind, user: AdminUser) -> ChannelUnlinked:
    """Unlink the channel of ``kind`` of member ``login``, whoever linked it and whatever its session."""
    _served_kind(kind)
    async with request.app.state.engine.begin() as conn:
        found = await member_channels.user_named(conn, login)
        gone = await member_channels.unlink(
            conn, user_id=found.id, kind=kind, by="admin", actor_id=user.user_id, actor_token_id=user.token_id
        )
    return ChannelUnlinked(login=found.login, kind=kind, unlinked=gone)


__all__ = ["ChannelLink", "ChannelLinked", "ChannelUnlinked", "router"]
