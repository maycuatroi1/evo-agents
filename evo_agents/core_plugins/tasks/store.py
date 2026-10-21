"""Reading the tasks of a project: the tasks a member sees (``tasks``, with filters), one task (``task``), the people
in each slot, a task's history and comments, the sprints, and the members a task may name (``project_members``).

A task carries a label (the project's default, or one an admin set), and it is read under that label: through the
sink X-Evo-Sink names, else the project's hub sink, as plans are read (``ProjectAccess.visible``). A task the member
cannot see answers the 404 of one that does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.tasks.blocker_queries import blocked
from evo_agents.core_plugins.tasks.settings import TasksError
from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.tables_tasks import PEOPLE_ROLES

SINK_HEADER = "X-Evo-Sink"  # evo_agents.hub.server.plans.SINK_HEADER
SLOTS = PEOPLE_ROLES[1:]  # the roles of a task beside its assignees: ba, qa, dev
MAX_LISTED = 2_000


@dataclass(frozen=True)
class TaskRow:
    """A task as its row and joins give it."""

    id: int
    number: int
    type: str
    title: str
    description_md: str
    outcome_md: str
    status: str
    priority: str
    due_on: date | None
    sprint: str | None
    parent_number: int | None
    label: dict
    external_ref: dict | None
    created_by: str
    created_at: datetime
    updated_at: datetime


@dataclass
class People:
    """Who stands in each slot of a task, by login."""

    assignees: list[str] = field(default_factory=list)
    roles: dict[str, list[str]] = field(default_factory=lambda: {slot: [] for slot in SLOTS})

    def logins(self) -> set[str]:
        return set(self.assignees).union(*self.roles.values())


@dataclass(frozen=True)
class EventRow:
    actor: str | None
    field: str
    from_value: str | None
    to_value: str | None
    cause: str
    at: datetime


@dataclass(frozen=True)
class CommentRow:
    id: int
    author: str
    body: str
    created_at: datetime


@dataclass(frozen=True)
class SprintRow:
    id: int
    name: str
    goal: str
    starts_on: date | None
    ends_on: date | None
    state: str
    created_by: str
    created_at: datetime
    closed_at: datetime | None
    activated_at: datetime | None
    tasks: int


@dataclass(frozen=True)
class MemberRow:
    """A member of the project, whom a task may name, with the role of their grant."""

    login: str
    role: str


@dataclass(frozen=True)
class Filters:
    """What a list of tasks keeps: a status, a sprint by name (or the tasks of none), the tasks a member stands in a
    slot of, the tasks an assignee has, the tasks that are blocked."""

    status: str | None = None
    sprint: str | None = None
    backlog: bool = False
    member_id: int | None = None
    assignee: str | None = None
    limit: int = 500
    blocked: bool = False


def sink_of(access: ProjectAccess, sink: str | None) -> str | None:
    """The sink a read goes through: the one named, else the project's hub sink."""
    if sink:
        return sink
    return access.rules.hub_sink.sink if access.rules.hub_sink else None


def visible(access: ProjectAccess, label, sink: str | None) -> bool:
    through = sink_of(access, sink)
    return through is not None and access.visible(label, through)


def reader(access: ProjectAccess) -> None:
    """Reading needs a grant on the project (a hub admin without one gets this far only)."""
    if access.role is None:
        raise TasksError(403, f"reading the tasks of project {access.name} needs a grant on it")


def writer(access: ProjectAccess, what: str) -> None:
    reader(access)
    if not has_role(access.role, "writer"):
        raise TasksError(403, f"{what} in project {access.name} needs the writer role; you are {access.role}")


def missing(access: ProjectAccess, ref: str) -> TasksError:
    return TasksError(404, f"no task {ref} in project {access.name} that you can see: see `evo-agents hub task list`")


def _tasks_query(project_id: int):
    t, s, u = tables.tasks.items, tables.tasks.sprints, tables.users
    parent = t.alias("parent")
    return (
        select(
            t.c.id,
            t.c.number,
            t.c.type,
            t.c.title,
            t.c.description_md,
            t.c.outcome_md,
            t.c.status,
            t.c.priority,
            t.c.due_on,
            s.c.name.label("sprint"),
            parent.c.number.label("parent_number"),
            t.c.label,
            t.c.external_ref,
            u.c.login.label("created_by"),
            t.c.created_at,
            t.c.updated_at,
        )
        .select_from(
            t.join(u, u.c.id == t.c.created_by)
            .outerjoin(s, s.c.id == t.c.sprint_id)
            .outerjoin(parent, parent.c.id == t.c.parent_id)
        )
        .where(t.c.project_id == project_id)
    )


def _filtered(query, filters: Filters):
    t, s, p, u = tables.tasks.items, tables.tasks.sprints, tables.tasks.people, tables.users
    if filters.status:
        query = query.where(t.c.status == filters.status)
    if filters.backlog:
        query = query.where(t.c.sprint_id.is_(None))
    elif filters.sprint:
        query = query.where(s.c.name == filters.sprint)
    if filters.member_id is not None:
        query = query.where(select(p.c.task_id).where(p.c.task_id == t.c.id, p.c.user_id == filters.member_id).exists())
    if filters.blocked:
        query = query.where(blocked())
    if filters.assignee:
        held = and_(p.c.task_id == t.c.id, p.c.role == "assignee", u.c.login == filters.assignee)
        query = query.where(select(p.c.task_id).join(u, u.c.id == p.c.user_id).where(held).exists())
    return query


async def all_tasks(conn: AsyncConnection, project_id: int) -> list[TaskRow]:
    """Every task of the project, by number, whoever reads: the knowledge graph reads them all and labels each by its
    row (``kg``)."""
    t = tables.tasks.items
    return [TaskRow(**row._mapping) for row in await conn.execute(_tasks_query(project_id).order_by(t.c.number))]


async def tasks(conn: AsyncConnection, access: ProjectAccess, sink: str | None, filters: Filters) -> list[TaskRow]:
    """The tasks of the project the member sees that ``filters`` keeps, by number, at most ``filters.limit``."""
    reader(access)
    query = _filtered(_tasks_query(access.project_id), filters).order_by(tables.tasks.items.c.number)
    rows = [TaskRow(**row._mapping) for row in await conn.execute(query)]
    return [row for row in rows if visible(access, row.label, sink)][: filters.limit]


async def task(
    conn: AsyncConnection, access: ProjectAccess, number: int, sink: str | None, *, lock: bool = False
) -> TaskRow:
    """Task ``number`` as the member sees it; 404 when there is none or they cannot see it. ``lock`` takes its row
    lock first, for a write."""
    reader(access)
    t = tables.tasks.items
    if lock:
        held = select(t.c.id).where(t.c.project_id == access.project_id, t.c.number == number).with_for_update()
        await conn.execute(held)
    row = (await conn.execute(_tasks_query(access.project_id).where(t.c.number == number))).first()
    if row is None or not visible(access, row.label, sink):
        raise missing(access, str(number))
    return TaskRow(**row._mapping)


async def people(conn: AsyncConnection, task_ids: list[int]) -> dict[int, People]:
    """task id -> who stands in each of its slots, each slot by login."""
    p, u = tables.tasks.people, tables.users
    query = (
        select(p.c.task_id, p.c.role, u.c.login)
        .join_from(p, u, u.c.id == p.c.user_id)
        .where(p.c.task_id.in_(task_ids or [0]))
        .order_by(p.c.task_id, p.c.role, u.c.login)
    )
    found = {task_id: People() for task_id in task_ids}
    for row in await conn.execute(query):
        slot = found[row.task_id]
        (slot.assignees if row.role == "assignee" else slot.roles[row.role]).append(row.login)
    return found


async def events(conn: AsyncConnection, task_id: int) -> list[EventRow]:
    """The history of a task, oldest first."""
    e, u = tables.tasks.events, tables.users
    query = (
        select(u.c.login.label("actor"), e.c.field, e.c.from_value, e.c.to_value, e.c.cause, e.c.at)
        .select_from(e.outerjoin(u, u.c.id == e.c.actor_id))
        .where(e.c.task_id == task_id)
        .order_by(e.c.at, e.c.id)
    )
    return [EventRow(**row._mapping) for row in await conn.execute(query)]


async def comments(conn: AsyncConnection, task_id: int) -> list[CommentRow]:
    """The comments on a task, oldest first."""
    c, u = tables.tasks.comments, tables.users
    query = (
        select(c.c.id, u.c.login.label("author"), c.c.body, c.c.created_at)
        .join_from(c, u, u.c.id == c.c.author_id)
        .where(c.c.task_id == task_id)
        .order_by(c.c.id)
    )
    return [CommentRow(**row._mapping) for row in await conn.execute(query)]


def _sprints_query(project_id: int):
    s, t, u = tables.tasks.sprints, tables.tasks.items, tables.users
    count = select(func.count()).where(t.c.sprint_id == s.c.id).scalar_subquery()
    return (
        select(
            s.c.id,
            s.c.name,
            s.c.goal,
            s.c.starts_on,
            s.c.ends_on,
            s.c.state,
            u.c.login.label("created_by"),
            s.c.created_at,
            s.c.closed_at,
            s.c.activated_at,
            count.label("tasks"),
        )
        .join_from(s, u, u.c.id == s.c.created_by)
        .where(s.c.project_id == project_id)
    )


async def sprints(conn: AsyncConnection, access: ProjectAccess) -> list[SprintRow]:
    """The sprints of the project, the newest first, each with how many tasks it holds (whatever their labels)."""
    reader(access)
    s = tables.tasks.sprints
    query = _sprints_query(access.project_id).order_by(s.c.starts_on.desc().nulls_first(), s.c.id.desc())
    return [SprintRow(**row._mapping) for row in await conn.execute(query)]


async def sprint(conn: AsyncConnection, project_id: int, name: str, *, lock: bool = False) -> SprintRow | None:
    """The sprint of the project named ``name``, or None."""
    s = tables.tasks.sprints
    query = _sprints_query(project_id).where(s.c.name == name)
    if lock:
        query = query.with_for_update(of=s)
    row = (await conn.execute(query)).first()
    return SprintRow(**row._mapping) if row else None


async def project_members(conn: AsyncConnection, access: ProjectAccess) -> list[MemberRow]:
    """Everyone who holds a grant on the project, by login: whom a task may name (``members``)."""
    reader(access)
    u, g = tables.users, tables.grants
    query = (
        select(u.c.login, g.c.role)
        .join_from(u, g, g.c.user_id == u.c.id)
        .where(g.c.project_id == access.project_id)
        .order_by(u.c.login)
    )
    return [MemberRow(row.login, row.role) for row in await conn.execute(query)]


async def members(conn: AsyncConnection, access: ProjectAccess, logins: set[str]) -> dict[str, int]:
    """login -> user id of each of ``logins`` who holds a grant on the project; 422 naming those who hold none."""
    if not logins:
        return {}
    u, g = tables.users, tables.grants
    query = (
        select(u.c.login, u.c.id)
        .join_from(u, g, g.c.user_id == u.c.id)
        .where(g.c.project_id == access.project_id, u.c.login.in_(sorted(logins)))
    )
    found = {row.login: row.id for row in await conn.execute(query)}
    if set(found) != logins:
        absent = ", ".join(sorted(logins - set(found)))
        raise TasksError(422, f"a task names members of project {access.name} only; no grant there: {absent}")
    return found


__all__ = [
    "SINK_HEADER",
    "SLOTS",
    "CommentRow",
    "EventRow",
    "Filters",
    "MemberRow",
    "People",
    "SprintRow",
    "TaskRow",
    "all_tasks",
    "comments",
    "events",
    "members",
    "missing",
    "people",
    "project_members",
    "reader",
    "sink_of",
    "sprint",
    "sprints",
    "task",
    "tasks",
    "visible",
    "writer",
]
