"""The label rule of a chat as values (``chats.service.access`` holds the module's design): the projects a chat's
label names (``LabelProject``), whether one person clears a label (``LabelReader``), one person's access to one chat
(``ChatAccess``), and the pure rules on them. Nothing here reads the database.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime

from evo_agents.hub import access, chats, telegram
from evo_agents.hub.server.security import holds

NOT_A_MEMBER = "no chat {id} that you are a member of: see `evo-agents hub chat list`"


SHARED_WITH = "shared_with"  # the key of a label's part that names the people it was shared with, by id


def shared_with(item) -> frozenset[int]:
    """The people a part of a label was shared with (``SHARED_WITH``); none for a part that names none or not as a list
    of ids."""
    found = item.get(SHARED_WITH) if isinstance(item, Mapping) else None
    if not isinstance(found, list):
        return frozenset()
    return frozenset(person for person in found if isinstance(person, int) and not isinstance(person, bool))


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
    shared_with: tuple[int, ...] = ()  # the people of the chat a writer on the project shared its part with

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
        if self.shared_with:
            found[SHARED_WITH] = sorted(set(self.shared_with))
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
            if self.user_id in shared_with(item):
                continue
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


def shares_part(project: LabelProject, role: str | None, max_level: str | None) -> bool:
    """Whether a person holding ``role`` up to ``max_level`` on ``project`` may share its part of a chat's label: the
    writer role and a grant that clears the part, shared with nobody else counted."""
    bare = replace(project, shared_with=())
    return holds(role, "writer") and bare.rules.visible_by_grant(bare.label, max_level)


def with_shares(projects: Iterable[LabelProject], shares: Iterable, chat_id: int) -> list[LabelProject]:
    """``projects`` of chat ``chat_id`` with the people each was shared with there (``label_shares``), counting only a
    share whose writer still holds the writer role on the project and a grant that clears its part, fail closed."""
    found = []
    for project in projects:
        people = {
            row.user_id
            for row in shares
            if row.chat_id == chat_id and row.project_id == project.id and shares_part(project, row.role, row.max_level)
        }
        found.append(replace(project, shared_with=tuple(sorted(people))))
    return found


def label_levels(chat: ChatAccess) -> list[str]:
    """The levels chat ``chat`` may be set to: on the ladder of every project of its agents, at or below each one's
    default level, lowest first; none for a direct chat or a chat without agents."""
    if chat.kind == chats.DIRECT or not chat.projects:
        return []
    return [level for level in chat.projects[0].levels if all(project.lowers_to(level) for project in chat.projects)]
