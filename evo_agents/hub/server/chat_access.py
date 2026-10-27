"""Who may read and write a group chat (``evo_agents.hub.chats``), and the label rule of chats.

Only the chat's members read it and write to it: someone who is not one, or left, gets the 404 of a chat that does not
exist, so an id tells nothing about the chats one is not in.

A chat's label is the labels of the projects of its agents: for each project with an agent in the chat, the project's
default label, the label its runs on no plan read as (``runs.service.views.sees_unplanned``), by names, naming that
project alone. A chat without agents has an empty label: it holds no project's work. A person clears a label when they
hold a grant on every project it names, whose max level reaches that project's label on the project's own ladder
(``ProjectRules.visible_by_grant``). Every person in a chat clears its label: adding an agent whose project would take
it past someone in the chat is refused, and so is adding someone who does not clear it. The hub checks it again on
every read and write, from the projects and grants as they are then, so someone whose grant was lowered or revoked is
refused (403), fail closed. A message carries the label the chat had when it was sent (``LabelReader``): once an agent
left and the label went down, a person who joined since does not read what was written before.

An admin of every project with an agent in a group chat may set the chat's own level (``chat_labels``): at or below
each project's default level, it replaces the level of the part of each project in the chat when it was set
(``chats.label_override``, {level, projects}), so the chat's label goes down and its messages may leave the hub at that
level (``channel_allows``). A project whose agent joins later keeps its default label, as no admin of it lowered it.
Every rule above reads the label with the level set: who may be in the chat, and the label a message gets.

A chat is restricted when a project of its agents is (``telegram.restricted``: its hub sink cleared at or above the
level customer or the location domestic-only, or no hub sink): its notices name the chat and link to it, no more. This
rule of a member's own channel reads the hub sink only, never a sink of kind channel.

A sink of kind channel (``access.CHANNEL_KIND``) names a kind of channel of a core plugin by its id (``telegram``,
say): what a chat of the project may send out to a group of that channel, read by ``channel_allows``. It fails closed:
a project without a sink of kind channel for that kind lets nothing of its out there.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import and_, func, literal, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import access, chats, tables, telegram
from evo_agents.hub.db import one_of
from evo_agents.hub.server.security import Principal

NOT_A_MEMBER = "no chat {id} that you are a member of: see `evo-agents hub chat list`"


@dataclass(frozen=True)
class LabelProject:
    """A project a chat's label names: its ladder, its default label, its hub sink's clearance, the clearances of its
    sinks of kind channel by id (the kind of channel each names), and the level a chat set for it, if any."""

    id: int
    name: str
    levels: list[str]
    locations: list[str]
    default_label: dict
    clearance: dict | None
    channels: dict = field(default_factory=dict)
    chat_level: str | None = None

    @property
    def rules(self) -> access.ProjectRules:
        return access.ProjectRules(self.name, self.levels, self.locations, [], self.default_label)

    @property
    def default_level(self) -> str | None:
        level = self.default_label.get("level")
        return level if isinstance(level, str) else None

    def lowers_to(self, level: str) -> bool:
        """Whether ``level`` is on the project's ladder at or below its default level, so a chat may set it."""
        default = self.default_level
        if level not in self.levels or default not in self.levels:
            return False
        return self.levels.index(level) <= self.levels.index(default)

    @property
    def label(self) -> dict:
        """The project's part of a chat's label: its default label, naming the project alone, at the level the chat
        set for it when there is one."""
        found = {**self.default_label, "projects": [self.name]}
        if self.chat_level is not None and self.lowers_to(self.chat_level):
            found["level"] = self.chat_level
        return found

    @property
    def lowered(self) -> bool:
        """Whether the chat set the project's part below its default level."""
        return self.label.get("level") != self.default_level

    @property
    def restricted(self) -> bool:
        return telegram.restricted(self.clearance, self.levels, self.locations)

    def channel_clears(self, item: Mapping, kind: str) -> bool:
        """Whether the project's sink of kind channel for ``kind`` clears ``item``, the project's part of a label; no
        such sink, or a clearance the ladder does not know, clears nothing."""
        clearance = self.channels.get(kind)
        if not isinstance(clearance, Mapping):
            return False
        sink = {"id": kind, "kind": access.CHANNEL_KIND, "clearance": dict(clearance)}
        rules = access.ProjectRules(self.name, self.levels, self.locations, [sink], self.default_label)
        return bool(self.levels) and rules.visible(item, self.levels[-1], kind)


def channel_allows(label, kind: str, projects: Mapping[str, LabelProject]) -> bool:
    """Whether a message carrying ``label`` may go out to a group of the channel ``kind``: every project the label
    names, found in ``projects`` by name, has a sink of kind channel for ``kind`` that clears its part. Fails closed:
    a label that is not a list, an empty one (no project let it out), a part naming no single known project, or a
    project without such a sink lets nothing out."""
    if not isinstance(label, list) or not label:
        return False
    for item in label:
        named = item.get("projects") if isinstance(item, Mapping) else None
        project = projects.get(named[0]) if isinstance(named, list) and len(named) == 1 else None
        if project is None or not project.channel_clears(item, kind):
            return False
    return True


async def channel_cleared(conn: AsyncConnection, label, kind: str) -> bool:
    """``channel_allows`` for ``label``, with the projects it names as the hub holds them now."""
    names = [
        item["projects"][0]
        for item in label or []
        if isinstance(item, Mapping) and isinstance(item.get("projects"), list) and item["projects"]
    ]
    return channel_allows(label, kind, await projects_named(conn, names))


def with_override(projects: Iterable[LabelProject], override) -> list[LabelProject]:
    """``projects`` with the level chat override ``override`` ({level, projects}, or None) sets for those it names."""
    level = override.get("level") if isinstance(override, Mapping) else None
    named = override.get("projects") if isinstance(override, Mapping) else None
    if not isinstance(level, str) or not isinstance(named, list):
        return [replace(project, chat_level=None) for project in projects]
    return [replace(project, chat_level=level if project.name in named else None) for project in projects]


Grants = dict[tuple[int, int], tuple[str, str]]  # (user id, project id): (role, max level)


@dataclass
class LabelReader:
    """Whether one person clears a label, for the projects ``projects`` names and the person's ``grants``."""

    user_id: int
    projects: dict[str, LabelProject]
    grants: Grants
    _seen: dict[str, bool] = field(default_factory=dict)

    def why_not(self, label: list[dict]) -> str | None:
        """Why the person does not clear ``label``; None when they do."""
        for item in label:
            named = item.get("projects") if isinstance(item, dict) else None
            project = self.projects.get(named[0]) if isinstance(named, list) and len(named) == 1 else None
            if project is None:
                return "the label names a project that is not on this hub any more"
            grant = self.grants.get((self.user_id, project.id))
            if grant is None:
                return f"no grant on project {project.name}"
            if not project.rules.visible_by_grant(item, grant[1]):
                level = item.get("level")
                return f"a grant on project {project.name} up to {grant[1]}, below its label {level}"
        return None

    def sees(self, label: list[dict]) -> bool:
        key = repr(label)
        if key not in self._seen:
            self._seen[key] = self.why_not(label) is None
        return self._seen[key]


@dataclass(frozen=True)
class ChatAccess:
    """One person in one chat: the chat, their membership, and the projects of its agents now."""

    chat_id: int
    title: str
    created_by: int
    creator: str
    created_at: datetime
    event_seq: int
    member_id: int
    last_read_seq: int
    user_id: int
    login: str
    projects: tuple[LabelProject, ...]
    kind: str = chats.GROUP  # chats.CHAT_KINDS: a direct chat's members never change
    label_override: dict | None = None  # chats.label_override: the level an admin set, and the projects it lowers

    @property
    def label_level(self) -> str | None:
        """The level set for the chat, None when its label is the projects' default."""
        level = (self.label_override or {}).get("level")
        return level if isinstance(level, str) else None

    @property
    def label(self) -> list[dict]:
        return chat_label(self.projects)

    @property
    def restricted(self) -> bool:
        return any(project.restricted for project in self.projects)

    @property
    def notice_project(self) -> int | None:
        """The project a notice of the chat is kept in: a restricted one when there is one, the first otherwise."""
        ordered = sorted(self.projects, key=lambda p: (not p.restricted, p.name))
        return ordered[0].id if ordered else None


def chat_label(projects: Iterable[LabelProject]) -> list[dict]:
    """The label of a chat whose agents are of ``projects``: their labels, by project name."""
    return [project.label for project in sorted({p.name: p for p in projects}.values(), key=lambda p: p.name)]


def _channels():
    """The clearances of a project's sinks of kind channel, as one JSONB object by id; {} without one."""
    p, cs = tables.projects, tables.project_sinks.alias("channel_sinks")
    found = func.jsonb_object_agg(cs.c.sink_id, cs.c.clearance, type_=JSONB)
    query = select(func.coalesce(found, literal({}, JSONB), type_=JSONB))
    return query.where(cs.c.project_id == p.c.id, cs.c.kind == access.CHANNEL_KIND).scalar_subquery()


def _projects():
    p, ps = tables.projects, tables.project_sinks
    hub_sink = and_(ps.c.project_id == p.c.id, ps.c.kind == access.HUB_KIND)
    return select(
        p.c.id,
        p.c.name,
        p.c.levels,
        p.c.locations,
        p.c.default_label,
        ps.c.clearance.label("clearance"),
        _channels().label("channels"),
    ).select_from(p.outerjoin(ps, hub_sink))


def _project(row) -> LabelProject:
    return LabelProject(
        id=row.id,
        name=row.name,
        levels=list(row.levels),
        locations=list(row.locations),
        default_label=dict(row.default_label),
        clearance=row.clearance,
        channels=dict(row.channels or {}),
    )


async def projects_named(conn: AsyncConnection, names: Iterable[str]) -> dict[str, LabelProject]:
    """The projects named ``names``, by name; a name with no project is left out."""
    wanted = sorted(set(names))
    if not wanted:
        return {}
    rows = await conn.execute(_projects().where(one_of(tables.projects.c.name, wanted)))
    return {row.name: _project(row) for row in rows}


async def projects_by_id(conn: AsyncConnection, ids: Iterable[int]) -> list[LabelProject]:
    wanted = sorted(set(ids))
    if not wanted:
        return []
    rows = await conn.execute(_projects().where(one_of(tables.projects.c.id, wanted)))
    return [_project(row) for row in rows]


async def label_override(conn: AsyncConnection, chat_id: int) -> dict | None:
    """The level set for chat ``chat_id`` and the projects it lowers (chats.label_override); None when unset."""
    c = tables.chats
    return (await conn.execute(select(c.c.label_override).where(c.c.id == chat_id))).scalar_one_or_none()


async def agent_projects(conn: AsyncConnection, chat_id: int) -> tuple[LabelProject, ...]:
    """The projects of the agents in chat ``chat_id`` now, at the level the chat set for each, if any."""
    m, ap = tables.chat_members, tables.agent_profiles
    query = select(ap.c.project_id).join_from(m, ap, ap.c.id == m.c.profile_id)
    query = query.where(m.c.chat_id == chat_id, m.c.removed_at.is_(None))
    ids = (await conn.execute(query)).scalars().all()
    found = with_override(await projects_by_id(conn, ids), await label_override(conn, chat_id)) if ids else []
    return tuple(sorted(found, key=lambda p: p.name))


async def cleared_chats(conn: AsyncConnection, user_id: int) -> list[int]:
    """The chats person ``user_id`` is in and clears the label of now, as ``chat_access`` lets them read, by id."""
    m, ap = tables.chat_members, tables.agent_profiles
    query = select(m.c.chat_id).where(m.c.user_id == user_id, m.c.removed_at.is_(None))
    chat_ids = sorted((await conn.execute(query)).scalars().all())
    if not chat_ids:
        return []
    query = select(m.c.chat_id, ap.c.project_id).join_from(m, ap, ap.c.id == m.c.profile_id)
    query = query.where(one_of(m.c.chat_id, chat_ids), m.c.removed_at.is_(None))
    agents = (await conn.execute(query)).all()
    projects = {p.id: p for p in await projects_by_id(conn, [row.project_id for row in agents])}
    grants = await grants_of(conn, [user_id], list(projects))
    by_chat: dict[int, list[LabelProject]] = {chat_id: [] for chat_id in chat_ids}
    for row in agents:
        if row.project_id in projects:
            by_chat[row.chat_id].append(projects[row.project_id])
    c = tables.chats
    query = select(c.c.id, c.c.label_override).where(one_of(c.c.id, chat_ids), c.c.label_override.is_not(None))
    overrides = {row.id: row.label_override for row in await conn.execute(query)}
    reader = LabelReader(user_id, {p.name: p for p in projects.values()}, grants)
    return [
        chat_id
        for chat_id, found in by_chat.items()
        if reader.why_not(chat_label(with_override(found, overrides.get(chat_id)))) is None
    ]


async def grants_of(conn: AsyncConnection, user_ids: Iterable[int], project_ids: Iterable[int]) -> Grants:
    users, projects = sorted(set(user_ids)), sorted(set(project_ids))
    if not users or not projects:
        return {}
    g = tables.grants
    query = select(g.c.user_id, g.c.project_id, g.c.role, g.c.max_level).where(
        one_of(g.c.user_id, users), one_of(g.c.project_id, projects)
    )
    return {(row.user_id, row.project_id): (row.role, row.max_level) for row in await conn.execute(query)}


async def uncleared(conn: AsyncConnection, user_ids: dict[int, str], projects: Iterable[LabelProject]) -> list[str]:
    """Why each person of ``user_ids`` (id: login) who does not clear the label of ``projects`` does not, in login
    order; empty when every one clears it."""
    chosen = {project.name: project for project in projects}
    grants = await grants_of(conn, user_ids, [p.id for p in chosen.values()])
    label = chat_label(chosen.values())
    found = []
    for user_id, login in sorted(user_ids.items(), key=lambda item: item[1].lower()):
        why = LabelReader(user_id, chosen, grants).why_not(label)
        if why is not None:
            found.append(f"{login} ({why})")
    return found


async def label_reader(conn: AsyncConnection, user_id: int, labels: Iterable[list]) -> LabelReader:
    """A reader of the labels ``labels`` for person ``user_id``: the projects they name and the person's grants."""
    names = {
        item["projects"][0]
        for label in labels
        for item in label
        if isinstance(item, dict) and isinstance(item.get("projects"), list) and item["projects"]
    }
    projects = await projects_named(conn, names)
    grants = await grants_of(conn, [user_id], [p.id for p in projects.values()])
    return LabelReader(user_id, projects, grants)


def _membership(chat_id: int, user_id: int):
    c, m, u = tables.chats, tables.chat_members, tables.users
    member = and_(m.c.chat_id == c.c.id, m.c.user_id == user_id, m.c.removed_at.is_(None))
    return (
        select(
            c.c.id,
            c.c.title,
            c.c.created_by,
            u.c.login.label("creator"),
            c.c.created_at,
            c.c.event_seq,
            m.c.id.label("member_id"),
            m.c.last_read_seq,
            c.c.kind,
            c.c.label_override,
        )
        .select_from(c.join(m, member).join(u, u.c.id == c.c.created_by))
        .where(c.c.id == chat_id)
    )


async def chat_access(conn: AsyncConnection, user: Principal, chat_id: int, *, lock: bool = False) -> ChatAccess:
    """``user``'s access to chat ``chat_id``: 404 unless they are in it, 403 when they no longer clear its label. With
    ``lock`` the chat's row is locked, as a change of its members needs."""
    query = _membership(chat_id, user.user_id)
    if lock:
        query = query.with_for_update(of=tables.chats)
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        raise HTTPException(404, NOT_A_MEMBER.format(id=chat_id))
    projects = await agent_projects(conn, chat_id)
    found = ChatAccess(
        chat_id=row.id,
        title=row.title,
        created_by=row.created_by,
        creator=row.creator,
        created_at=row.created_at,
        event_seq=row.event_seq,
        member_id=row.member_id,
        last_read_seq=row.last_read_seq,
        user_id=user.user_id,
        login=user.login,
        projects=projects,
        kind=row.kind,
        label_override=row.label_override,
    )
    grants = await grants_of(conn, [user.user_id], [p.id for p in projects])
    why = LabelReader(user.user_id, {p.name: p for p in projects}, grants).why_not(found.label)
    if why is not None:
        raise HTTPException(
            403,
            f"chat {chat_id} holds the work of projects {', '.join(p.name for p in projects)}, and you no longer "
            f"clear its label: {why}. Ask an admin of the project for a grant that reaches it",
        )
    return found


def label_levels(chat: ChatAccess) -> list[str]:
    """The levels chat ``chat`` may be set to: on the ladder of every project of its agents, at or below each one's
    default level, lowest first; none for a direct chat or a chat without agents."""
    if chat.kind == chats.DIRECT or not chat.projects:
        return []
    return [level for level in chat.projects[0].levels if all(project.lowers_to(level) for project in chat.projects)]


async def label_admin(conn: AsyncConnection, chat: ChatAccess) -> bool:
    """Whether the reader of ``chat`` is an admin of every project of its agents, as setting its level needs."""
    if not chat.projects:
        return False
    grants = await grants_of(conn, [chat.user_id], [p.id for p in chat.projects])
    return all(grants.get((chat.user_id, p.id), (None,))[0] == "admin" for p in chat.projects)
