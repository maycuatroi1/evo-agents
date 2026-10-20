"""The API of the hub for core plugins, version 1.0: the one module of ``evo_agents.hub`` a core plugin imports
(``from evo_agents.hub.server import core_plugin_api as hub``), whose ``__all__`` is the whole of it. ``docs/plugins.md``
(The core plugin API) is the contract; ``tests/plugins/golden/core-plugin-api.json`` holds its surface.

A core plugin runs in the hub's processes and holds the hub's connection: the API makes the right code easy to write
and stable from one release to the next, it is no sandbox. Each function that acts for a member takes a ``Member``,
which only this module makes, from a channel of the plugin's bound to a live web session, and goes through the very
code the web route that does the same goes through, with its checks; what the hub refuses is ``Refused``, with the
status and the words of that route. A mistake of the plugin's (a kind of channel it does not contribute, a config too
large, a handle it made itself) is a ValueError.

- The ground: ``require``, ``plugin``, the callers of the plugin's routes (``WebSession``, ``HubAdmin``),
  ``begin``, ``hub_config``, ``web_url``, ``Context`` and ``transaction`` for its jobs, events, tools and sources,
  ``plugin_environment``, and the class and errors of the extension point channels.
- A member's channel: ``find_member``, ``link_channel``, ``unlink_channel``, ``link_code``, ``redeem_link_code``,
  ``direct_chat_member``, ``set_channel_state``, ``delivered_notification``, and ``prepare`` with ``restricted`` for
  what a ``Clearance`` keeps out.
- What a member does from it: ``answer_decision``, ``answer_proposal``, ``agents``, ``open_direct_chat``,
  ``send_message``, ``read_message``; the chat functions need the project of each agent to grant the plugin
  ``chats:write`` or ``chats:read``.
- The plugin's own: ``plugin_config`` (its effective config, x-secret fields unsealed), ``granted``, and
  ``chat_history_items`` for a source of the knowledge graph.

Versions. ``require(major, minor)`` passes when this hub serves exactly that version, and raises UnsupportedApi
naming both otherwise; a plugin calls it in the function its entry point names, so a hub of another version leaves the
plugin out with that reason. A change that only adds to the surface takes the next minor version, one that removes or
changes anything the next major version, minor 0; ``python -m evo_agents.hub.server.core_plugin_api surface`` prints
the surface, ``... version`` the version.
"""

from __future__ import annotations

import sys
from typing import Annotated

from fastapi import Depends

from evo_agents.hub.server.admin import Login
from evo_agents.hub.server.core_plugin_api._acts import (
    agents,
    answer_decision,
    answer_proposal,
    chat_history_items,
    open_direct_chat,
    read_message,
    send_message,
)
from evo_agents.hub.server.core_plugin_api._base import (
    Context,
    begin,
    granted,
    hub_admin,
    hub_config,
    plugin,
    plugin_config,
    plugin_environment,
    restricted,
    transaction,
    web_session,
    web_url,
)
from evo_agents.hub.server.core_plugin_api._channels import (
    delivered_notification,
    direct_chat_member,
    find_member,
    link_channel,
    link_code,
    prepare,
    redeem_link_code,
    set_channel_state,
    unlink_channel,
)
from evo_agents.hub.server.core_plugin_api._types import (
    AgentRef,
    ChatMessage,
    Clearance,
    DecisionAnswer,
    DirectChat,
    KgSourceAnswer,
    LinkCode,
    Member,
    NotACorePlugin,
    NotificationRef,
    Plugin,
    PluginConfig,
    ProposalAnswer,
    Refused,
    SentMessage,
    Session,
    UnsupportedApi,
)
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.notifications import (
    SESSION_ENDED,
    Channel,
    ChannelGone,
    Outgoing,
    RetryAfter,
    Undeliverable,
)

API_VERSION: tuple[int, int] = (1, 0)

WebSession = Annotated[Session, Depends(web_session)]
HubAdmin = Annotated[Session, Depends(hub_admin)]


def require(major: int, minor: int) -> None:
    """Raise UnsupportedApi naming both versions unless this hub serves exactly API major.minor."""
    for value in (major, minor):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("require takes the major and the minor version as integers")
    if (major, minor) != API_VERSION:
        caller_module = sys._getframe(1).f_globals.get("__name__", "a core plugin")
        served = ".".join(map(str, API_VERSION))
        raise UnsupportedApi(f"{caller_module} needs core plugin API {major}.{minor}; this hub serves {served}")


__all__ = [
    "API_VERSION",
    "SESSION_ENDED",
    "AgentRef",
    "Channel",
    "ChannelGone",
    "ChatMessage",
    "Clearance",
    "Context",
    "DecisionAnswer",
    "DirectChat",
    "ErrorBody",
    "HubAdmin",
    "KgSourceAnswer",
    "LinkCode",
    "Login",
    "Member",
    "NotACorePlugin",
    "NotificationRef",
    "Outgoing",
    "Plugin",
    "PluginConfig",
    "ProposalAnswer",
    "Refused",
    "RetryAfter",
    "SentMessage",
    "Session",
    "Undeliverable",
    "UnsupportedApi",
    "WebSession",
    "agents",
    "answer_decision",
    "answer_proposal",
    "begin",
    "chat_history_items",
    "delivered_notification",
    "direct_chat_member",
    "find_member",
    "granted",
    "hub_config",
    "link_channel",
    "link_code",
    "open_direct_chat",
    "plugin",
    "plugin_config",
    "plugin_environment",
    "prepare",
    "read_message",
    "redeem_link_code",
    "require",
    "restricted",
    "send_message",
    "set_channel_state",
    "transaction",
    "unlink_channel",
    "web_url",
]
