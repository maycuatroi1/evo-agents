"""The words and shapes of a project's decision board (plan loop-engine, criterion a8): what an item of the board is, who
may decide it, and the members of the project by role. ``board`` gathers the items, ``board_gates`` the three gates and
``board_chain`` what the Loop's chain waits on.

An item says what waits (``kind``, in its ``group``), since when, who may decide it (``decided_by``, a rule in a word,
and ``deciders``, the logins that may now) and where (``link``, the page of the hub's web whose buttons call the
object's own route). Its ``title`` is the hub's own line, from codes, numbers and states, and from a question or a
summary only where the member reading the board may read it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.plugin_calls import enabled_in
from evo_agents.hub.server.plugin_configs import stored_stacks
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.plugins import config as plugin_config

GROUPS = ("goal", "canvas", "policy", "exception", "srs")
GOAL, CANVAS, POLICY, EXCEPTION, SRS = GROUPS
KINDS_OF = {
    GOAL: ("goal_review", "goal_overdue"),
    CANVAS: ("baseline", "claim_proposal"),
    POLICY: ("policy_proposal", "goal_proposal", "merge_breaker", "loop_breaker", "srs_baseline"),
    EXCEPTION: ("loop_decision", "run_decision", "author_question", "merge_held", "merge_manual", "merge_sample"),
    SRS: ("srs_review", "srs_finding"),
}
KINDS = tuple(kind for kinds in KINDS_OF.values() for kind in kinds)
GROUP_OF = {kind: group for group, kinds in KINDS_OF.items() for kind in kinds}
# Who may decide an item: an admin of the project; a writer (or an admin), of a canvas's baseline one who proposed
# none of its items (any writer once the plugin canvas's self_approval is on); the member a run stands for; a writer
# who wrote none of the SRS versions under review (any writer once the plugin srs's self_approval is on); a member of
# the settle chat.
DECIDED_BY = ("admin", "writer", "run_owner", "srs_reviewer", "chat_member")
ADMIN, WRITER, RUN_OWNER, SRS_REVIEWER, CHAT_MEMBER = DECIDED_BY
TITLE_CHARS = 200
QUOTED_CHARS = 160  # of a question or a summary in a title


class BoardItem(BaseModel):
    """One thing of the project that waits for a person."""

    group: Literal[GROUPS] = Field(
        description="goal, canvas and policy are the three gates; exception and srs what the Loop's chain waits on"
    )
    kind: Literal[KINDS]
    title: str = Field(description="what waits, in one line")
    since: datetime | None = Field(description="when it began to wait")
    decided_by: Literal[DECIDED_BY] = Field(description="who may decide it, as a rule")
    deciders: list[str] = Field(description="the logins of the members who may decide it now")
    link: str | None = Field(description="the page of the hub's web where it is decided")
    work_item: int | None = Field(None, description="the Loop's work item that waits on it")
    refs: dict[str, int | str] = Field(
        default_factory=dict, description="the ids of what waits: decision, run, proposal, baseline, plan, goal..."
    )


class DecisionBoard(BaseModel):
    """Everything of a project that waits for a person, the three gates first, the oldest first in each group."""

    project: str
    loop: bool = Field(description="the policy's loop.enabled: the Loop makes work items")
    counts: dict[Literal[GROUPS], int] = Field(description="the items of each group")
    items: list[BoardItem]


def item(kind: str, title: str, since, decided_by: str, deciders: Iterable[str], link, **extra) -> BoardItem:
    """A board item of ``kind``, its title made one line."""
    return BoardItem(
        group=GROUP_OF[kind],
        kind=kind,
        title=line(title, TITLE_CHARS),
        since=since,
        decided_by=decided_by,
        deciders=sorted(set(deciders)),
        link=link,
        **extra,
    )


def line(text, limit: int = QUOTED_CHARS) -> str:
    """``text`` as one line of at most ``limit`` characters."""
    found = " ".join("".join(ch if ch.isprintable() else " " for ch in str(text or "")).split())
    return found if len(found) <= limit else found[: limit - 3].rstrip() + "..."


@dataclass(frozen=True)
class Member:
    user_id: int
    login: str
    role: str


@dataclass(frozen=True)
class People:
    """The members of a project who hold a grant on it."""

    members: tuple[Member, ...]

    def holding(self, role: str, *, but: Iterable[int] = ()) -> list[str]:
        """The logins of the members holding ``role`` or above, but those whose ids are ``but``."""
        left_out = set(but)
        return [m.login for m in self.members if has_role(m.role, role) and m.user_id not in left_out]

    def logins(self, user_ids: Iterable[int]) -> list[str]:
        """The logins of the members among ``user_ids`` who still hold a grant."""
        wanted = set(user_ids)
        return [m.login for m in self.members if m.user_id in wanted]


SELF_APPROVAL = "self_approval"  # the field of the configs of srs and canvas that lets a proposer decide too


async def self_approval(conn: AsyncConnection, plugin: str, project_id: int) -> bool:
    """Whether core plugin ``plugin``, enabled in the project, lets the member who proposed a thing decide it too (its
    config's self_approval, as srs and canvas read it); False when the plugin is not enabled."""
    row = await enabled_in(conn, plugin, project_id)
    if row is None:
        return False
    stacks = await stored_stacks(conn, [row.id], project_id, None)
    field = plugin_config.merge(row.manifest.get("config"), stacks.get(row.id, [])).fields.get(SELF_APPROVAL)
    return field is not None and field.value is True


async def people(conn: AsyncConnection, project_id: int) -> People:
    g, u = tables.grants, tables.users
    query = (
        select(u.c.id, u.c.login, g.c.role)
        .join_from(g, u, u.c.id == g.c.user_id)
        .where(g.c.project_id == project_id)
        .order_by(u.c.login)
    )
    return People(tuple(Member(row.id, row.login, row.role) for row in await conn.execute(query)))


@dataclass(frozen=True)
class Reading:
    """What the member reading the board may read: the runs of the plans ``plans`` and, with ``unplanned``, those of
    no plan, as GET .../decisions reads them (``views.visible_plans``, ``views.sees_unplanned``); and an object under
    its label through the sink ``through`` (the one the request named, else the project's hub sink)."""

    access: ProjectAccess
    plans: frozenset[str]
    unplanned: bool
    through: str | None

    @property
    def project(self) -> str:
        return self.access.name

    @property
    def project_id(self) -> int:
        return self.access.project_id

    def run(self, plan_id: str | None) -> bool:
        """Whether the member reads a run, or a decision, of plan ``plan_id`` (None: of no plan)."""
        return plan_id in self.plans if plan_id is not None else self.unplanned

    def label(self, *labels) -> bool:
        """Whether the member reads an object carrying each of ``labels``."""
        return self.through is not None and all(self.access.visible(label, self.through) for label in labels)


def sink_of(access: ProjectAccess, sink: str | None) -> str | None:
    """The sink a read goes through: the one named, else the project's hub sink (as ``plans._sink``)."""
    if sink:
        return sink
    return access.rules.hub_sink.sink if access.rules.hub_sink else None


# The pages of the hub's web an item links to


def project_path(project: str, *parts: str) -> str:
    return "/".join([f"/p/{quote(project, safe='')}", *(quote(str(part), safe="") for part in parts)])


def canvas_path(project: str, slug: str, *parts: str) -> str:
    """A canvas of the plugin canvas, or one of its pages."""
    return project_path(project, "x", "canvas", "canvases", slug, *parts)


def document_path(project: str, slug: str) -> str:
    """A document of the plugin srs (``hub.server.srs.store.document_path``)."""
    return project_path(project, "x", "srs", "documents", slug)


def board_path(project: str) -> str:
    """The decision board itself, where the Loop's notices link."""
    return project_path(project, "decide")


def ordered(items: Sequence[BoardItem]) -> list[BoardItem]:
    """The gates first, then the exceptions and the SRS; the oldest first in each group, an item with no time last."""

    def key(found: BoardItem):
        since = found.since.timestamp() if found.since is not None else float("inf")
        return (GROUPS.index(found.group), since, KINDS.index(found.kind), found.title)

    return sorted(items, key=key)


__all__ = [
    "ADMIN",
    "CHAT_MEMBER",
    "DECIDED_BY",
    "GROUPS",
    "KINDS",
    "RUN_OWNER",
    "SRS_REVIEWER",
    "WRITER",
    "BoardItem",
    "DecisionBoard",
    "People",
    "Reading",
    "board_path",
    "canvas_path",
    "document_path",
    "item",
    "line",
    "ordered",
    "people",
    "project_path",
    "self_approval",
    "sink_of",
]
