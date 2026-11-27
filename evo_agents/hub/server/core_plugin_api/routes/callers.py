"""The callers of a core plugin's routes, as FastAPI dependencies: the web session, any member, a hub admin, a
transaction on the hub's engine and the hub's config."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.config import HubConfig
from evo_agents.hub.server.core_plugin_api.models.types import Refused, Session
from evo_agents.hub.server.core_plugin_api.service.handles import issue_session
from evo_agents.hub.server.notifications.models import FROM_SESSION
from evo_agents.hub.server.security import UNSAFE_METHODS, WEB, AdminUser, CurrentUser
from evo_agents.hub.server.terminal import origin_of

NOT_FROM_THIS_HUB = (
    "a write made with a web session comes from a page of this hub: its Origin header must be {origin}, the origin of "
    "EVO_HUB_PUBLIC_URL"
)
NO_PUBLIC_URL = "this hub has no EVO_HUB_PUBLIC_URL, so it cannot tell its own pages' writes from another site's"


def _from_this_hub(request: Request, user: CurrentUser) -> None:
    """403 unless a write carries the Origin of the hub's public URL. An app session passes: it arrives only as
    ``Authorization: Bearer``, which a browser never adds to another site's request, and its app is no page of this hub
    (``security.Principal.app_session``)."""
    if request.method not in UNSAFE_METHODS or user.app_session:
        return
    expected = origin_of(request.app.state.config.public_url)
    if expected is None:
        raise Refused(403, NO_PUBLIC_URL)
    if origin_of(request.headers.get("origin")) != expected:
        raise Refused(403, NOT_FROM_THIS_HUB.format(origin=expected))


def web_session(request: Request, user: CurrentUser) -> Session:
    """The caller's web session: 403 for any other credential, and for a write whose Origin is not the hub's."""
    if user.kind != WEB:
        raise Refused(403, FROM_SESSION)
    _from_this_hub(request, user)
    return issue_session(user.user_id, user.login, user.token_id, True, principal=user)


def caller(user: CurrentUser) -> Session:
    """The caller, a member signed in with any credential the hub's own routes take: ``web`` for a web session, which
    alone links a channel. The hub's middleware has checked a web session's write for its CSRF header."""
    return issue_session(user.user_id, user.login, user.token_id, user.kind == WEB, principal=user)


def hub_admin(request: Request, user: AdminUser) -> Session:
    """The caller, a hub admin (403 otherwise, as the hub's admin routes answer), with a web session or a machine
    token; a write made with a web session carries the Origin of the hub's public URL."""
    if user.kind == WEB:
        _from_this_hub(request, user)
    return issue_session(user.user_id, user.login, user.token_id, user.kind == WEB, admin=True, principal=user)


def begin(request: Request) -> AbstractAsyncContextManager[AsyncConnection]:
    """A transaction on the hub's engine, for a route of the plugin."""
    return request.app.state.engine.begin()


def hub_config(request: Request) -> HubConfig:
    """The hub's config, which a plugin hands the API and never reads."""
    return request.app.state.config
