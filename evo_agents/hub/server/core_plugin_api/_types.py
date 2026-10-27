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
    ``evo_agents.kg.policy`` reads a clearance that names none). With ``below`` (API 1.1) it carries what sits under
    them on the ladder, not at them, and on a ladder without the level or location, its lowest rung alone: Telegram's
    is ``Clearance("customer", "domestic-only", below=True)``."""

    level: str
    location: str | None = None
    below: bool = False


@dataclass(frozen=True, eq=False)
class Session:
    """A live credential of a member, from ``WebSession``, ``Caller``, ``HubAdmin`` or ``redeem_link_code``: ``web``
    for a web session, which alone links a channel (``Caller`` and ``HubAdmin`` also take a machine token). From
    ``run_session`` (API 1.3) it is the member a run of the plugin acts for, ``machine`` and without a token of its
    own (``token_id`` 0)."""

    user_id: int
    login: str
    token_id: int
    web: bool = True
    machine: bool = False


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
class LinkedChannel:
    """A member's channel of a kind as the hub keeps it (API 1.1)."""

    config: Mapping[str, Any]  # as stored, its "state" and the hub's "disabled_reason" included
    linked_at: datetime
    enabled: bool  # the hub sends to it: it is on, and the web session that linked it lives
    disabled_reason: str | None  # why the hub does not: what turned it off, or SESSION_ENDED


@dataclass(frozen=True)
class Unlinked:
    """What a hub admin's unlink of a member's channel did (API 1.1)."""

    login: str  # the member's login as the hub keeps it
    unlinked: bool  # a channel was linked, and is not any more


@dataclass(frozen=True)
class LinkCode:
    code: str  # one time, for ten minutes
    expires_at: datetime


@dataclass(frozen=True)
class Bridge:
    """A group chat of the hub bridged to a group of the plugin's channel (API 1.5)."""

    id: int
    chat_id: int
    kind: str
    external_id: str  # the group's id on the channel
    config: Mapping[str, Any]  # what the plugin linked it with
    state: Mapping[str, Any]  # what the plugin keeps of it, ``bridge_set_state``


@dataclass(frozen=True)
class BridgeFile:
    """A file a member attached to a message of a bridged chat (API 1.5): ``bridge_file`` reads its bytes. Never a
    run's output or a file a tool read."""

    id: int
    name: str
    media_type: str
    size: int  # in bytes


@dataclass(frozen=True)
class BridgeMessage:
    """A message of a bridged chat as its group may read it (API 1.5): the text redacted, with the files its author
    attached to it (``files``), never a run's outputs."""

    id: int
    chat_id: int
    bridge_id: int
    thread_root_id: int | None
    author_kind: Literal["person", "agent", "hub"]
    author: str | None  # a person's login, an agent's handle; None for the hub
    body: str
    run_id: int | None
    via: str | None  # the kind of channel it came in by, None when sent on the hub
    reply_to: str | None  # the group's message its thread started from, when the bridge carried it
    link: str  # the message's page on the hub, a path: ``web_url`` makes it absolute, for what the group does not carry
    created_at: datetime
    files: tuple[BridgeFile, ...] = ()  # what its author attached, in the order uploaded; ``bridge_file`` reads each


class _KgSourceItems(TypedDict):
    items: list
    identifiers: list


class KgSourceAnswer(_KgSourceItems, total=False):
    """What a source of the knowledge graph answers a build (``evo_agents.hub.kg_sources``)."""

    cursor: dict


# API 1.3: access, the hub's data, the plugin's writes, agents, notices, configs and invites


@dataclass(frozen=True)
class ProjectRef:
    """A project as a caller reaches it: its id, its name, and the caller's role and highest level there (None for a
    hub admin without a grant)."""

    id: int
    name: str
    role: str | None
    max_level: str | None = None


@dataclass(frozen=True)
class UserRef:
    id: int
    login: str
    created_at: datetime
    last_seen_at: datetime | None


@dataclass(frozen=True)
class RunRef:
    id: int
    project: str
    kind: str | None
    state: str
    title: str | None
    plan_id: str | None
    step_key: str | None
    dispatched_by: str | None  # the member's login
    queued_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class GrantRef:
    login: str
    user_id: int
    role: str
    max_level: str
    granted_at: datetime


@dataclass(frozen=True)
class PlanRef:
    plan_id: str
    area: str | None
    revision: int
    status: str | None  # the plan body's status, when it has one
    title: str | None
    label: Mapping[str, Any]
    updated_at: datetime


@dataclass(frozen=True)
class StepRef:
    plan_id: str
    key: str  # step_key: its id, else its order, else its position
    index: int
    title: str | None
    status: str | None
    depends_on: tuple[str, ...]
    data: Mapping[str, Any]  # the step as the plan holds it


@dataclass(frozen=True)
class DecisionRef:
    id: int
    run_id: int | None
    plan_id: str | None
    step_key: str | None
    category: str | None
    question: str
    state: str
    answer_option: str | None
    asked_at: datetime
    answered_at: datetime | None


@dataclass(frozen=True)
class SignalValue:
    id: int
    key: str
    value: float | None
    text: str | None
    source: str | None
    tenant: str | None
    measured_at: datetime
    label: Mapping[str, Any]


@dataclass(frozen=True)
class GoalRef:
    id: int
    code: str  # G-<number>
    alias: str | None
    revision: int
    statement: str
    metric: Mapping[str, Any] | None
    metric_text: str  # how the metric reads
    deadline: Any  # a date, or None
    status: str
    owner: str | None


@dataclass(frozen=True)
class ClaimRef:
    id: int
    canvas_id: int
    number: int
    cell: str
    text: str
    status: str
    confidence: str | None
    rev: int
    label: Mapping[str, Any]
    updated_at: datetime


@dataclass(frozen=True)
class TaskRef:
    id: int
    number: int  # its code is the project's task prefix, -, then this
    type: str
    title: str
    status: str
    priority: str | None
    due_on: Any  # a date, or None
    sprint_id: int | None
    parent_id: int | None
    label: Mapping[str, Any]
    updated_at: datetime


@dataclass(frozen=True)
class SprintRef:
    id: int
    name: str
    goal: str | None
    state: str
    starts_on: Any
    ends_on: Any


@dataclass(frozen=True)
class TaskLinkRef:
    task_id: int
    kind: str
    srs_item_id: int | None
    plan_id: str | None
    step_id: str | None
    run_id: int | None
    url: str | None


@dataclass(frozen=True)
class SrsItemRef:
    id: int
    document: str  # its document's slug
    key: str
    kind: str
    code: str | None
    created_in: int | None
    retired_in: int | None


@dataclass(frozen=True)
class SrsVersionRef:
    document: str
    number: int
    state: str
    summary: str | None
    created_at: datetime
    decided_at: datetime | None


@dataclass(frozen=True)
class AgentTypeRef:
    """An agent type of the plugin as a project enabled it."""

    name: str
    handle: str
    profile_id: int
    enabled: bool  # on, and its profile not deleted


@dataclass(frozen=True)
class ChatRunRef:
    """The chat run a tool or a job of the plugin is called in: its chat, the thread it answers in and the member at
    the origin of the chain, whom it acts for."""

    run_id: int
    chat_id: int
    root_id: int
    profile_id: int
    dispatcher_id: int
    dispatcher: str  # their login


@dataclass(frozen=True)
class TaskHanded:
    """What ``hand_task`` did: the task run it queued, or why it queued none."""

    run_id: int | None
    refused: str | None


@dataclass(frozen=True)
class SignalPushed:
    """What ``push_signal`` did with a value: stored it, found it stored already, or refused it, with why."""

    stored: bool
    duplicate: bool
    refused: str | None


@dataclass(frozen=True)
class Invite:
    id: int
    token: str  # shown this once: the hub keeps its hash
    url: str | None  # the web's page of it, when the hub knows its public URL
    expires_at: datetime


@dataclass(frozen=True)
class ProjectConfig:
    """The plugin's effective config in one project that enables it."""

    project: str
    values: Mapping[str, Any]  # x-secret fields unsealed
    version: int | None  # the version of the project's layer, None without one
    missing_secrets: tuple[str, ...] = ()


# The handles the API made


_PLUGINS: dict[str, Plugin] = {}  # plugin id: the handle plugin() gave
_SESSIONS: weakref.WeakSet[Session] = weakref.WeakSet()
_ADMINS: weakref.WeakSet[Session] = weakref.WeakSet()
_MEMBERS: weakref.WeakSet[Member] = weakref.WeakSet()
_PRINCIPALS: weakref.WeakKeyDictionary[Session, Principal] = weakref.WeakKeyDictionary()  # as the hub checks them


def issue_plugin(plugin_id: str, kinds: tuple[str, ...]) -> Plugin:
    """The handle of core plugin ``plugin_id``: the one given before while its kinds are the same."""
    known = _PLUGINS.get(plugin_id)
    if known is not None and known.kinds == kinds:
        return known
    made = _PLUGINS[plugin_id] = Plugin(plugin_id, kinds)
    return made


def issue_session(
    user_id: int, login: str, token_id: int, web: bool, *, admin: bool = False, principal: Principal | None = None
) -> Session:
    machine = principal is not None and principal.kind == "machine"
    made = Session(user_id, login, token_id if token_id is not None else 0, web, machine)
    _SESSIONS.add(made)
    if admin:
        _ADMINS.add(made)
    if principal is not None:
        _PRINCIPALS[made] = principal
    return made


def session_principal(session: Session) -> Principal:
    """The session as the hub's checks take it: the credential it came from, or, for a session of before 1.3 the API
    made without one, a member without a token who is no hub admin."""
    found = _PRINCIPALS.get(session)
    if found is not None:
        return found
    return Principal(
        user_id=session.user_id,
        login=session.login,
        admin=session in _ADMINS,
        token_id=session.token_id or None,
        kind="web" if session.web else "machine",
        token_hash="",
    )


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
        raise ValueError(
            f"{kind!r} is not a kind of channel plugin {plugin.id} contributes ({', '.join(plugin.kinds)})"
        )
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
    if not isinstance(cleared, Clearance) or not isinstance(cleared.below, bool):
        raise ValueError("a clearance is a Clearance(level, location, below=False)")
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
