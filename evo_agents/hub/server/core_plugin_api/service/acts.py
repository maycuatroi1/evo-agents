"""What a member does from a core plugin's channel through the API: answer a decision or a proposal, open a direct
chat, send and read messages, list the agents to talk to; and the plugin's source of chat history. Each function goes
through the code the web route that does the same goes through, with its checks, and adds the feature and the grant
the plugin needs. Private to ``core_plugin_api``."""

from __future__ import annotations

import logging
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import access, features, telegram
from evo_agents.hub.kg_sources import source_id
from evo_agents.hub.redact import redact
from evo_agents.hub.server import hub_settings
from evo_agents.hub.server.chats.models.access import ChatAccess
from evo_agents.hub.server.chats.models.messages import ChatMessageIn
from evo_agents.hub.server.chats.repository.views import message_rows, profiles_by_id
from evo_agents.hub.server.chats.service import graph as chat_graph
from evo_agents.hub.server.chats.service.access import chat_access
from evo_agents.hub.server.chats.service.direct import direct_chat
from evo_agents.hub.server.chats.service.messages import deliver_message, visible_message
from evo_agents.hub.server.core_plugin_api.models.types import (
    AgentRef,
    ChatMessage,
    Clearance,
    DecisionAnswer,
    DirectChat,
    KgSourceAnswer,
    Member,
    Plugin,
    ProposalAnswer,
    Refused,
    SentMessage,
)
from evo_agents.hub.server.core_plugin_api.repository.acts import agents_of, holds_grant
from evo_agents.hub.server.core_plugin_api.repository.ground import project_id_of
from evo_agents.hub.server.core_plugin_api.service.ground import project_grants
from evo_agents.hub.server.core_plugin_api.service.handles import (
    own_clearance,
    own_member,
    own_plugin,
    plugin_kinds,
    principal_of,
    refusing,
)
from evo_agents.hub.server.decisions.models import AnswerIn
from evo_agents.hub.server.decisions.service import answers as decision_answers
from evo_agents.hub.server.notifications.service import member_channels
from evo_agents.hub.server.profile_store import by_handle
from evo_agents.hub.server.proposals.models import ProposalAnswer as ProposalAnswerIn
from evo_agents.hub.server.proposals.service.reads import answer_proposal_as
from evo_agents.hub.server.security import Principal, holds

READS, WRITES = "chats:read", "chats:write"
NOT_GRANTED = "project {project} does not grant plugin {plugin} {permission}: an admin of the project grants it"
NO_PROJECT = "chat {chat} has no agent of a project, so no project granted plugin {plugin} {permission}"
KEPT_OFF = "chat {chat} holds the work of project {project}, which keeps it off this channel: read it on the hub"
# The 422 of an answer in words that ``redact`` would mask in part. A channel tells it from the other 422s by these
# exact words (``docs/plugins.md``, answer_decision); they name no value.
HOLDS_SECRET = (
    "the answer looks like it holds a secret (a token, a key or a password), so the hub kept none of it: "
    "answer again without it, and give a credential on the hub"
)

log = logging.getLogger(__name__)


def _via(member: Member, via: str | None) -> str:
    """What the audit row of an answer names: the channel's kind, or another kind of channel of the same plugin."""
    if via is None:
        return member.kind
    if via not in plugin_kinds(member.plugin):
        raise ValueError(f"via names a kind of channel of plugin {member.plugin}, not {via!r}")
    return via


async def feature_on(conn: AsyncConnection, user: Principal, feature_id: str, project: str | None = None) -> None:
    """404, as FeatureGate answers the feature's routes, when ``feature_id`` is off (in ``project``)."""
    message = await hub_settings.refusal(conn, user, features.BY_ID[feature_id], project)
    if message is not None:
        raise Refused(404, message)


async def granting(conn: AsyncConnection, plugin: str, project: str, permission: str) -> None:
    if not await project_grants(conn, plugin, project, permission):
        raise Refused(403, NOT_GRANTED.format(project=project, plugin=plugin, permission=permission))


async def chat_grants(conn: AsyncConnection, plugin: str, chat: ChatAccess, permission: str) -> None:
    """403 unless the project of every agent of the chat grants the plugin ``permission``; a chat without agents
    belongs to no project that could."""
    if not chat.projects:
        raise Refused(403, NO_PROJECT.format(chat=chat.chat_id, plugin=plugin, permission=permission))
    for project in chat.projects:
        await granting(conn, plugin, project.name, permission)


# Decisions and proposals


async def answer_decision(
    conn: AsyncConnection,
    member: Member,
    decision_id: int,
    cleared: Clearance,
    *,
    option: str | None = None,
    text: str | None = None,
    via: str | None = None,
) -> DecisionAnswer:
    """Answer decision ``decision_id`` as the member, with an option, words or both, through the code of the web's
    answer route: first 404 without a grant on its project (the same words whether it exists or not) and 403 when the
    decision's label keeps it off the channel (its project's sink of kind channel named as the member's channel), or,
    for a decision without a label, when the project is kept from a channel cleared for ``cleared``; 403 for anyone
    but the person it is asked of (or one it was escalated to); then 422 for a body the route refuses, 403 unless the member
    dispatched its run, 409 for one no longer open, 422 for an option it lacks, 409 for a run that takes no answer, 403
    for a worker that takes steering from the web only. Words that ``evo_agents.hub.redact`` masks any part of are
    422 with ``HOLDS_SECRET`` before anything else, and nothing is kept or logged of them. Audited with
    ``via=<via>``, the channel's kind by default, which the decision keeps in ``answered_via``."""
    own_member(member)
    own_clearance(cleared)
    said = _via(member, via)
    if text is not None and redact(text) != text:
        log.info(
            "channel answer refused: it looks like it holds a secret", extra={"decision_id": decision_id, "via": said}
        )
        raise Refused(422, HOLDS_SECRET)
    user = principal_of(member)
    with refusing():
        project = await member_channels.answerable_project(
            conn,
            member.user_id,
            "decision",
            decision_id,
            cleared.level,
            cleared.location,
            cleared.below,
            channel=member.kind,
        )
        body = AnswerIn(option=option, text=text)  # pyright: ignore[reportCallIssue]
        answered = await decision_answers.answer_decision(conn, user, project, decision_id, body, via=said)
    chosen = next((item for item in answered.decision.options if item.key == option), None) if option else None
    label = None if chosen is None else chosen.label
    return DecisionAnswer(decision_id, option, label, answered.run_id, answered.inbox_run)


async def answer_proposal(
    conn: AsyncConnection,
    member: Member,
    proposal_id: int,
    cleared: Clearance,
    action: Literal["accept", "reject", "defer"],
    *,
    defer_days: int | None = None,
    note: str | None = None,
    via: str | None = None,
) -> ProposalAnswer:
    """Accept, reject or defer proposal ``proposal_id`` as the member, through the code of the web's answer route:
    404 and 403 for its project as ``answer_decision``, 404 while the feature curator is off in the project, 422 for a
    body the route refuses (``defer_days`` 1 to 90, with defer), 403 without the admin role on the project, 409 for a
    proposal neither open nor deferred. Audited with ``via=<via>``."""
    own_member(member)
    own_clearance(cleared)
    said = _via(member, via)
    user = principal_of(member)
    with refusing():
        project = await member_channels.answerable_project(
            conn, member.user_id, "proposal", proposal_id, cleared.level, cleared.location, cleared.below
        )
        await feature_on(conn, user, "curator", project)
        body = ProposalAnswerIn(action=action, defer_days=defer_days, note=note)
        proposal = await answer_proposal_as(conn, user, project, proposal_id, body, via=said)
    return ProposalAnswer(proposal.id, action, proposal.state)


# Chats


async def open_direct_chat(conn: AsyncConnection, member: Member, handle: str) -> DirectChat:
    """The member's direct chat with agent ``handle``, the one there is or one made now, through the code of PUT
    /v1/chats/direct/{handle}: 404 while the feature chats is off; 403 unless the agent's project grants the plugin
    chats:write; then the route's 404 for an agent the member cannot see, 403 without the writer role on its project,
    409 for a deleted one, a handle that is the member's login, or a project's label the member does not clear."""
    own_member(member)
    user = principal_of(member)
    with refusing():
        await feature_on(conn, user, "chats")
        profile = await by_handle(conn, handle) if isinstance(handle, str) else None
        if profile is not None and await holds_grant(conn, member.user_id, profile.project_id):
            await granting(conn, member.plugin, profile.project, WRITES)
        chat_id, made = await direct_chat(conn, user, handle)
    return DirectChat(chat_id, made)


async def send_message(
    conn: AsyncConnection, member: Member, chat_id: int, body: str, *, reply_to: int | None = None
) -> SentMessage:
    """Send ``body`` to chat ``chat_id`` as the member, in the thread of ``reply_to`` when given, through the code of
    POST /v1/chats/{id}/messages: 404 while the feature chats is off, 422 for a body the route refuses (some text, 16
    KiB of UTF-8 at most), 404 for a chat the member is not in and 403 for one whose label they no longer clear, 403
    unless the project of every agent of the chat grants the plugin chats:write; then the route's mentions, runs,
    answers of the hub, notices and audit row."""
    own_member(member)
    user = principal_of(member)
    with refusing():
        await feature_on(conn, user, "chats")
        message = ChatMessageIn(body=body, reply_to=reply_to)
        chat = await chat_access(conn, user, chat_id)
        await chat_grants(conn, member.plugin, chat, WRITES)
        message_id = await deliver_message(conn, chat, user, message)
        (row,) = await message_rows(conn, chat.chat_id, [message_id])
    return SentMessage(row.id, row.seq, row.thread_root_id)


async def read_message(
    conn: AsyncConnection, member: Member, chat_id: int, message_id: int, cleared: Clearance
) -> ChatMessage:
    """Message ``message_id`` of chat ``chat_id`` as the member reads it on the web: 404 while the feature chats is
    off, 404 and 403 for the chat as ``send_message``, 403 unless the project of every agent of the chat grants the
    plugin chats:read, 403 when one of those projects is kept from a channel cleared for ``cleared`` (the plugin sends
    a link instead), and 404 for a message the member does not clear the label of, or none by that id."""
    own_member(member)
    own_clearance(cleared)
    user = principal_of(member)
    with refusing():
        await feature_on(conn, user, "chats")
        chat = await chat_access(conn, user, chat_id)
        await chat_grants(conn, member.plugin, chat, READS)
        for project in chat.projects:
            if telegram.restricted_for(
                project.clearance,
                project.levels,
                project.locations,
                cleared.level,
                cleared.location,
                below=cleared.below,
            ):
                raise Refused(403, KEPT_OFF.format(chat=chat.chat_id, project=project.name))
        row = await visible_message(conn, chat, message_id)
    author_kind, author = "hub", None
    if row.author_profile_id is not None:
        profiles = await profiles_by_id(conn, [row.author_profile_id])
        author_kind, author = "agent", profiles[row.author_profile_id].handle
    elif row.author_login is not None:
        author_kind, author = "person", row.author_login
    elif row.guest_name is not None:
        author_kind, author = "guest", row.guest_name
    return ChatMessage(
        row.id, row.chat_id, row.seq, row.thread_root_id, author_kind, author, row.body, row.run_id, row.created_at
    )


async def agents(conn: AsyncConnection, member: Member) -> list[AgentRef]:
    """The agents the member may talk to: not deleted, of the projects where they hold the writer role or above, by
    project then the default first; 404 while the feature chats is off."""
    own_member(member)
    with refusing():
        await feature_on(conn, principal_of(member), "chats")
    writers = [role for role in access.ROLES if holds(role, "writer")]
    return [AgentRef(row.handle, row.name, row.project) for row in await agents_of(conn, member.user_id, writers)]


async def chat_history_items(conn: AsyncConnection, plugin: Plugin, project: str) -> KgSourceAnswer:
    """What the plugin's source of the knowledge graph answers a build of ``project``: the messages of the group chats
    whose label holds a part of the project, with their threads and chats, labels and audiences, as the hub makes them
    (``evo_agents.hub.server.chats.service.graph``); nothing while the project does not grant the plugin chats:read."""
    own_plugin(plugin)
    project_id = await project_id_of(conn, project)
    if project_id is None:
        return {"items": [], "identifiers": []}
    return await chat_graph.read(conn, plugin.id, source_id(plugin.id), project_id, project)  # pyright: ignore[reportReturnType]
