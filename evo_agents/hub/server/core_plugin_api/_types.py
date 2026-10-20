"""The types of the API of core plugins, its errors, and the handles only the API makes (``Plugin``, ``Session``,
``Member``), which its functions take back and check they made: a plugin cannot hand the API a member, a session or
another plugin's handle it built itself through the API's own names. Private to ``core_plugin_api``: a plugin imports
the package, never this module."""

from __future__ import annotations

import weakref
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal, TypedDict

from fastapi import HTTPException
from pydantic import ValidationError

from evo_agents.hub.server.security import Principal


class Refused(HTTPException):
    """What the hub refuses, with the status and the words the web route that does the same answers: in a route of
    the plugin it is that response; elsewhere the plugin tells its user ``detail``."""


class UnsupportedApi(RuntimeError):  # noqa: N818 (what the hub says of a plugin, as ImportError would)
    """The plugin was written for another version of the API than the one this hub serves."""


class NotACorePlugin(LookupError):  # noqa: N818
    """The module that asked is in no core plugin's package."""


@dataclass(frozen=True, eq=False)
class Plugin:
    """A core plugin, as ``plugin(__name__)`` gives it: its id and the kinds of channel its manifest contributes."""

    id: str
    kinds: tuple[str, ...]


@dataclass(frozen=True)
class Clearance:
    """What a channel may carry, on each project's own ladder: a level, and a location (None: any location, as
    ``evo_agents.kg.policy`` reads a clearance that names none)."""

    level: str
    location: str | None = None


@dataclass(frozen=True, eq=False)
class Session:
    """A live credential of a member, from ``WebSession``, ``HubAdmin`` or ``redeem_link_code``: ``web`` for a web
    session, which alone links a channel (``HubAdmin`` also takes a hub admin's machine token)."""

    user_id: int
    login: str
    token_id: int
    web: bool = True


@dataclass(frozen=True, eq=False)
class Member:
    """The member a channel of the plugin speaks for, from ``find_member``, ``link_channel`` or
    ``direct_chat_member``: never a hub admin and without a token, whoever the member is."""

    user_id: int
    login: str
    plugin: str
    kind: str
    channel_id: int
    config: Mapping[str, Any]  # the channel's config as stored, its "state" included


@dataclass(frozen=True)
class PluginConfig:
    values: Mapping[str, Any]  # the effective config, x-secret fields unsealed
    missing_secrets: tuple[str, ...]  # secret fields this process cannot open


@dataclass(frozen=True)
class DirectChat:
    chat_id: int
    made: bool  # made now, rather than the one there was


@dataclass(frozen=True)
class SentMessage:
    id: int
    seq: int
    thread_root_id: int | None


@dataclass(frozen=True)
class ChatMessage:
    id: int
    chat_id: int
    seq: int
    thread_root_id: int | None
    author_kind: Literal["person", "agent", "hub"]
    author: str | None  # a person's login, an agent's handle; None for the hub
    body: str
    run_id: int | None  # the run of the agent that wrote it
    created_at: datetime


@dataclass(frozen=True)
class DecisionAnswer:
    decision_id: int
    option: str | None
    option_label: str | None
    run_id: int
    inbox_run: int  # the run whose inbox took the answer: the decision's, or the one that resumed it


@dataclass(frozen=True)
class ProposalAnswer:
    proposal_id: int
    action: str
    state: str


@dataclass(frozen=True)
class NotificationRef:
    id: int
    kind: str
    project: str | None
    decision_id: int | None
    proposal_id: int | None


@dataclass(frozen=True)
class AgentRef:
    handle: str
    name: str
    project: str


@dataclass(frozen=True)
class LinkCode:
    code: str  # one time, for ten minutes
    expires_at: datetime


class _KgSourceItems(TypedDict):
    items: list
    identifiers: list


class KgSourceAnswer(_KgSourceItems, total=False):
    """What a source of the knowledge graph answers a build (``evo_agents.hub.kg_sources``)."""

    cursor: dict


# The handles the API made


_PLUGINS: dict[str, Plugin] = {}  # plugin id: the handle plugin() gave
_SESSIONS: weakref.WeakSet[Session] = weakref.WeakSet()
_ADMINS: weakref.WeakSet[Session] = weakref.WeakSet()
_MEMBERS: weakref.WeakSet[Member] = weakref.WeakSet()


def issue_plugin(plugin_id: str, kinds: tuple[str, ...]) -> Plugin:
    """The handle of core plugin ``plugin_id``: the one given before while its kinds are the same."""
    known = _PLUGINS.get(plugin_id)
    if known is not None and known.kinds == kinds:
        return known
    made = _PLUGINS[plugin_id] = Plugin(plugin_id, kinds)
    return made


def issue_session(user_id: int, login: str, token_id: int, web: bool, *, admin: bool = False) -> Session:
    made = Session(user_id, login, token_id, web)
    _SESSIONS.add(made)
    if admin:
        _ADMINS.add(made)
    return made


def issue_member(plugin: str, kind: str, user_id: int, login: str, channel_id: int, config: Mapping) -> Member:
    made = Member(user_id, login, plugin, kind, channel_id, MappingProxyType(dict(config)))
    _MEMBERS.add(made)
    return made


def own_plugin(plugin) -> Plugin:
    if not isinstance(plugin, Plugin) or _PLUGINS.get(plugin.id) is not plugin:
        raise ValueError("a Plugin comes from plugin(__name__) only")
    return plugin


def own_kind(plugin: Plugin, kind: str) -> str:
    if kind not in plugin.kinds:
        raise ValueError(f"{kind!r} is not a kind of channel plugin {plugin.id} contributes ({', '.join(plugin.kinds)})")
    return kind


def own_session(session) -> Session:
    if not isinstance(session, Session) or session not in _SESSIONS:
        raise ValueError("a Session comes from WebSession, HubAdmin or redeem_link_code only")
    return session


def admin_session(session) -> Session:
    if not isinstance(session, Session) or session not in _ADMINS:
        raise ValueError("an unlink by an admin names the Session HubAdmin gave")
    return session


def own_member(member) -> Member:
    if not isinstance(member, Member) or member not in _MEMBERS:
        raise ValueError("a Member comes from find_member, link_channel or direct_chat_member only")
    return member


def own_clearance(cleared) -> Clearance:
    if not isinstance(cleared, Clearance):
        raise ValueError("a clearance is a Clearance(level, location)")
    return cleared


def plugin_kinds(plugin_id: str) -> tuple[str, ...]:
    known = _PLUGINS.get(plugin_id)
    return () if known is None else known.kinds


def principal_of(member: Member) -> Principal:
    """The member as the hub's checks take them: no token, of the channel's kind, and never a hub admin."""
    return Principal(
        user_id=member.user_id, login=member.login, admin=False, token_id=None, kind=member.kind, token_hash=""
    )


def _validation_text(exc: ValidationError) -> str:
    found = exc.errors()
    if not found:
        return str(exc)
    first = found[0]
    where = ".".join(str(part) for part in first.get("loc", ()))
    return f"{where}: {first.get('msg', 'invalid')}" if where else str(first.get("msg", "invalid"))


@contextmanager
def refusing() -> Iterator[None]:
    """The hub's refusals as ``Refused``: an HTTPException of a route's code with its status and words, and a body
    the route's model refuses as 422."""
    try:
        yield
    except Refused:
        raise
    except HTTPException as exc:
        raise Refused(exc.status_code, exc.detail) from None
    except ValidationError as exc:
        raise Refused(422, _validation_text(exc)) from None
