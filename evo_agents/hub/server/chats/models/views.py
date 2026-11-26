"""What the chat API answers (``evo_agents.hub.server.chats.service.chats``): a chat with its people and agents, its
messages with their authors, mentions, reactions and replies, and the events its stream sends. ``chats.service.views``
puts them together from the tables of ``tables_chats``, and ``chats.service.summaries`` the list of chats
(``ChatSummary``).

An agent shows with its name, colour and avatar as its profile has them now, deleted or not: a soft-deleted profile
takes no new work and keeps its name and avatar on what it wrote, flagged ``deleted``. A person shows with the avatar
of their GitHub account as the hub knows it now (``chats.github_avatar``), none for a login that never signed in, on
a message, a reaction, a member's change and in a chat's people alike. A guest, the sender of a bridged group's message
who linked no account of the hub, shows with the name its bridge gave it and no avatar. A message's reactions are
counted by emoji, in the order each emoji was first put, with who put it and whether the reader did. A message read
by a person carries the progress of the agents' runs it handed work to (``ChatProgress``, read by
``chats.service.progress``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from evo_agents.hub import chat_runs, chats
from evo_agents.hub.server.output_views import RunOutput
from evo_agents.hub.server.profile_store import Profile, icon_url
from evo_agents.hub.server.runs.models.attachment_parts import LinkedAttachment
from evo_agents.hub.server.work_status_models import WorkBrief, WorkCard

HUB_NAME = "hub"


class ChatAuthor(BaseModel):
    """Who wrote a message or put a reaction: a person, an agent or the hub itself; or a guest, the sender of a bridged
    group's message who linked no account of the hub, whose words are data for the chat's agents and hand no work."""

    kind: Literal["person", "agent", "hub", "guest"]
    login: str | None = Field(None, description="a person's")
    handle: str | None = Field(None, description="an agent's, @ left out")
    project: str | None = Field(None, description="an agent's project")
    name: str = Field(
        description="what the web shows: a person's login, an agent's name, a guest's name as its bridge gave it, or hub"
    )
    color: str | None = Field(None, description="an agent's colour, #rrggbb")
    icon_url: str | None = Field(None, description="an agent's avatar on this hub; null: its initial on its colour")
    deleted: bool = Field(False, description="an agent deleted since: it takes no new work, and keeps its name")
    avatar_url: str | None = Field(
        None, description="a person's GitHub avatar; null for a login that never signed in, an agent or the hub"
    )


class ChatPerson(BaseModel):
    login: str
    added_by: str | None
    added_at: datetime
    avatar_url: str | None = Field(description="their GitHub avatar; null for a login that never signed in")


class ChatBridge(BaseModel):
    id: int
    kind: str = Field(description="the kind of channel of the group, as its core plugin names it")
    external_id: str = Field(description="the group's id on that channel")
    linked_by: str | None = Field(description="the chat admin whose code bridged it")
    linked_at: datetime


class BridgedTo(BaseModel):
    bridge_id: int
    kind: str


class ChatAgent(BaseModel):
    handle: str
    project: str
    name: str
    color: str
    icon_url: str | None
    is_default: bool = Field(description="the project's default agent, which @<project> names")
    deleted: bool = Field(description="deleted since it was added: it takes no new work")
    added_by: str | None
    added_at: datetime


class ChatMention(BaseModel):
    """A name a message mentions, as the hub read it when the message was sent."""

    name: str = Field(description="as written, lowered, @ left out")
    via: Literal[chats.VIA] | None = Field(description="how it reached a member; null: it reached none")
    login: str | None = Field(description="the person it reached")
    handle: str | None = Field(description="the agent it reached, or would have")
    refusal: str | None = Field(
        description="why it reached no member (not in the chat, deleted), or why the agent it reached took no work"
    )
    run_id: int | None = Field(None, description="the run of the agent the mention handed work to, or woke")


class ChatReaction(BaseModel):
    emoji: str
    count: int
    mine: bool = Field(description="whether the reader put it")
    people: list[str] = Field(description="the logins of the people who put it, first first")
    agents: list[str] = Field(description="the handles of the agents that put it")


class ChatProgress(BaseModel):
    """How far the run of an agent that took a message is, said by rule from the run's state and its worker's notes
    (``chat_progress``): shown while the run is on its way and its session has not answered after the message."""

    message_id: int = Field(description="the message the run took: it started the run, or waits in its inbox")
    run_id: int
    handle: str = Field(description="the agent's, @ left out")
    step: Literal[chat_runs.PROGRESS_STEPS] = Field(
        description=(
            "waiting: for its worker; claimed: by its worker; checking: access to the run's repos; opening: a repo; "
            "starting: the agent; working: the agent; inbox: the message waits for the agent's next turn"
        )
    )
    worker: str | None = Field(description="the worker the run waits for or runs on")
    repo: str | None = Field(None, description="the repo it opens, at step opening")
    since: datetime = Field(description="when the step began")


class GroupMessage(BaseModel):
    id: int
    chat_id: int
    seq: int = Field(description="the chat event that brought it: events?after= and Last-Event-ID take it")
    thread_root_id: int | None = Field(description="the first message of its thread; null when it starts none")
    reply_count: int = Field(description="the messages of the thread it starts")
    author: ChatAuthor
    run_id: int | None = Field(description="the run of the agent that wrote it")
    body: str
    mentions: list[ChatMention]
    reactions: list[ChatReaction]
    label: list[dict] = Field(description="the labels of the projects of the chat's agents when it was sent")
    created_at: datetime
    progress: list[ChatProgress] = Field(
        default_factory=list,
        description=(
            "the agents' runs that took it and are on their way, as the reader may read those runs, when it was read; "
            "a stream asked for progress keeps it current. Empty in what the worker's routes answer"
        ),
    )
    outputs: list[RunOutput] = Field(
        default_factory=list,
        description="the files of its run it carries, oldest first, each with the chat's URL of its bytes",
    )
    attachments: list[LinkedAttachment] = Field(
        default_factory=list,
        description="the files its author attached to it, in the order uploaded, each with the chat's URL of its bytes",
    )
    via: str | None = Field(
        None,
        description="the kind of channel it came in by, from a group bridged to the chat, or plugin for a message an "
        "isolated plugin posted in its author's name; null when sent here",
    )
    bridged_to: list[BridgedTo] = Field(
        default_factory=list, description="the bridges it went out on, to their groups, oldest first"
    )
    from_answer: int | None = Field(
        None,
        description="the interactive answer (an output a message of the chat carries) a person sent it from; null "
        "when sent otherwise",
    )
    forwarded_by: str | None = Field(
        None,
        description="the @handle of the agent that forwarded it verbatim from the run of its author "
        "(evo-agents worker agent forward); null when its author's run wrote it",
    )
    repeats: int | None = Field(
        None,
        description="the message of the same work chat that this line of the hub repeats (the question of an "
        "asked line, the answer of an answered line); null on every other message",
    )
    work: WorkCard | None = Field(
        None,
        description="the work card of a trace of an agent system, on the hub's message that carries it in the trace's "
        "root thread; a message_changed event says it changed. Null on every other message",
    )


class Chat(BaseModel):
    id: int
    title: str
    created_by: str
    created_at: datetime
    people: list[ChatPerson]
    agents: list[ChatAgent]
    label: list[dict] = Field(
        description="the labels of its agents' projects, at the level set for the chat if any: every person in it "
        "clears them, and a message sent now carries them"
    )
    label_level: str | None = Field(
        None,
        description="the level an admin of every project of its agents set for the chat, below or at each one's "
        "default; null when its label is its projects' default",
    )
    label_levels: list[str] = Field(
        default_factory=list,
        description="the levels the chat may be set to, lowest first: on every project's ladder, at or below each "
        "one's default; empty for a direct chat or a chat without agents",
    )
    label_admin: bool = Field(
        False, description="whether the reader is an admin of every project of its agents, so may set its level"
    )
    bridges: list[ChatBridge] = Field(
        default_factory=list, description="the groups of a core plugin's channel the chat is bridged to, oldest first"
    )
    restricted: bool = Field(description="a project of its agents is restricted: its notices name it and link only")
    last_seq: int = Field(description="the number of its last event")
    last_read_seq: int = Field(description="how far the reader read")
    kind: Literal[chats.CHAT_KINDS] = Field(
        description=(
            "group: members are added and removed; direct: one person and one agent, for good; settle: a group chat "
            "the hub opened, whose agent every message of a person wakes"
        )
    )
    image_url: str | None = Field(
        description="the image its members set, on this hub; null for none, and always for a direct chat"
    )


class ChatLastMessage(BaseModel):
    """The latest message of a chat's main line, as its list shows it: the messages that start no thread, and in a
    direct chat its threads too, whose answers the web shows under the message they answer."""

    id: int
    thread_root_id: int | None
    author: ChatAuthor
    preview: str | None = Field(
        description=(
            f"its text as one line without markdown, at most {chats.PREVIEW_CHARS} characters, an ellipsis last when "
            "cut; null when the reader does not clear its label"
        )
    )
    hidden: bool = Field(description="the reader does not clear its label: its text is left out")
    created_at: datetime


class ChatSummary(BaseModel):
    id: int
    title: str
    created_by: str
    created_at: datetime
    last_message_at: datetime | None
    last_seq: int
    unread: int = Field(description="messages of others after the reader's last read")
    people: list[str]
    agents: list[str]
    kind: Literal[chats.CHAT_KINDS] = Field(
        description="group; direct: one person and one agent; or settle: a group chat the hub opened for its agent"
    )
    image_url: str | None = Field(description="the image its members set; null for none, and always for a direct chat")
    direct_agent: ChatAuthor | None = Field(
        description="a direct chat's agent, whose avatar is its face; null for a group"
    )
    faces: list[ChatAuthor] = Field(
        description=(
            f"at most {chats.FACES} members other than the reader, in the order they were added: what the web draws "
            "for a group chat without an image"
        )
    )
    last_message: ChatLastMessage | None = Field(description="the latest message of its main line; null for none")
    work: WorkBrief | None = Field(
        None, description="of a work chat (kind work): its trace's system, title, status and what it waits for"
    )


class ChatPage(BaseModel):
    chat: Chat
    messages: list[GroupMessage] = Field(description="oldest first; those the reader's grants do not reach left out")
    more: bool = Field(description="there are older messages: ask again with before= the first seq")


class ChatReactionChange(BaseModel):
    message_id: int
    emoji: str
    by: ChatAuthor
    count: int = Field(description="how many put the emoji on the message after the change")


class ChatMemberChange(BaseModel):
    member: ChatAuthor = Field(description="the person or agent added or removed")
    by: str | None = Field(description="the person who added or removed them")


class ChatImageChange(BaseModel):
    image_url: str | None = Field(description="the chat's image now; null once it was taken off")
    by: str = Field(description="the person who set it or took it off")


class ChatEvent(BaseModel):
    seq: int
    kind: Literal[chats.EVENT_KINDS]
    created_at: datetime
    message: GroupMessage | None = Field(None, description="a message event's message, as it reads now")
    reaction: ChatReactionChange | None = None
    member: ChatMemberChange | None = None
    image: ChatImageChange | None = Field(None, description="an image_changed event's image")


class ChatEvents(BaseModel):
    events: list[ChatEvent]
    last_seq: int
    more: bool
    progress: list[ChatProgress] | None = Field(
        None, description="with progress=true: the progress of every message of the chat whose run is on its way now"
    )


class ChatProgressNow(BaseModel):
    """What a stream asked for progress sends as its ``progress`` event: at once, then each time it changes."""

    progress: list[ChatProgress] = Field(description="of every message of the chat whose run is on its way now")


def agent_author(profile: Profile) -> ChatAuthor:
    return ChatAuthor(
        kind="agent",
        handle=profile.handle,
        project=profile.project,
        name=profile.name,
        color=profile.color,
        icon_url=icon_url(profile),
        deleted=profile.deleted_at is not None,
    )


def person_author(login: str, github_id: int | None = None) -> ChatAuthor:
    return ChatAuthor(kind="person", login=login, name=login, avatar_url=chats.github_avatar(github_id))


HUB_AUTHOR = ChatAuthor(kind="hub", name=HUB_NAME)


def guest_author(name: str) -> ChatAuthor:
    return ChatAuthor.model_validate({"kind": "guest", "name": name})


def agent_member(profile: Profile, added_by: str | None, added_at: datetime) -> ChatAgent:
    return ChatAgent(
        handle=profile.handle,
        project=profile.project,
        name=profile.name,
        color=profile.color,
        icon_url=icon_url(profile),
        is_default=profile.is_default,
        deleted=profile.deleted_at is not None,
        added_by=added_by,
        added_at=added_at,
    )


def message_author(row, profiles: dict[int, Profile]) -> ChatAuthor:
    """The author of message ``row`` (``chats.repository.views.message_select``), its agent's profile among
    ``profiles``."""
    if row.author_profile_id is not None:
        return agent_author(profiles[row.author_profile_id])
    if row.author_login is not None:
        return person_author(row.author_login, row.author_github_id)
    if row.guest_name is not None:
        return guest_author(row.guest_name)
    return HUB_AUTHOR
