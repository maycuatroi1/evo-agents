"""The tables of the core plugin tasks (0047), which ``evo_agents.hub.tables`` adds to its metadata as ``tables.tasks``:
queries name them there (``tables.tasks.items``). The plugin's code is ``evo_agents.core_plugins.tasks``; the hub owns
the tables because its one chain of migrations creates them (``docs/plugins.md``, Core plugins).

- tasks_sprints: a sprint of a project: its name (unique in the project), goal, dates and state (planned, active,
  closed); a project has at most one active sprint. ``activated_at`` is when it turned active (0085; NULL for a
  sprint that was active before, or is not yet).
- tasks_items: a task of a project: its number (1 and up within the project, shown with the config's prefix, ``T-42``),
  type, title, description, status (a key of the config's statuses), priority, due date, sprint, parent, the label it
  carries (the project's by default), and where an import took it from (``external_key``, unique in the project, and
  ``external_ref``, which also keeps the codes and steps the import could not link).
- tasks_people: who stands in each slot of a task: its assignees and its BA, QA and Dev; each holds a grant on the
  project when they are set.
- tasks_links: what a task links to: an item of the plugin srs (by id, so a new code or title keeps the link), a step
  of a plan (by plan id and step id, its status read from the plan each time), a run, or a pull or merge request (URL).
- tasks_events: the history of a task: a field changed, by whom (NULL: the hub), from what to what and why; an event
  the hub wrote for a plugin delivery names it, so a delivery tried twice writes it once.
- tasks_comments: comments on a task.
- tasks_blockers: what a task waits on (0085), a flag beside its status: why (``reason``), and who or what it waits
  for in one of three forms (a member with a grant on the project, another task of the project, or a text for
  someone outside), since when, who set it, and, once cleared, when and by whom. A task with a blocker that is not
  cleared is blocked. An open decision of a run that belongs to the task is a blocker too, but it is read, never a row.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    func,
    or_,
)
from sqlalchemy.dialects.postgresql import JSONB

SPRINT_STATES = ("planned", "active", "closed")  # tasks_sprints.state
PRIORITIES = ("urgent", "high", "medium", "low")  # tasks_items.priority
PEOPLE_ROLES = ("assignee", "ba", "qa", "dev")  # tasks_people.role
LINK_KINDS = ("srs_item", "plan_step", "run", "pull")  # tasks_links.kind
BLOCKER_WAITING = ("waiting_user_id", "waiting_task_id", "waiting_text")  # tasks_blockers: exactly one is set
MAX_WAITING_CHARS = 300  # of a waiting_text
MAX_REASON_CHARS = 1_000  # of a blocker
KEY = r"^[a-z][a-z0-9_]{0,39}$"  # tasks_items.status and type: a key of the plugin's config
MAX_TITLE_CHARS = 300  # of a task
MAX_DESCRIPTION_CHARS = 100_000
MAX_SPRINT_NAME_CHARS = 100
MAX_GOAL_CHARS = 2_000
MAX_URL_CHARS = 2_000
MAX_EXTERNAL_KEY_CHARS = 300
MAX_PLAN_ID_CHARS = 200
MAX_STEP_CHARS = 100  # a plan's step id, as a run names it
MAX_FIELD_CHARS = 40  # of an event
MAX_VALUE_CHARS = 1_000
MAX_CAUSE_CHARS = 500
MAX_COMMENT_CHARS = 20_000


@dataclass(frozen=True)
class TasksTables:
    """The tables of the core plugin tasks, by what each holds."""

    sprints: Table
    items: Table
    people: Table
    links: Table
    events: Table
    comments: Table
    blockers: Table


def _key(table: str, column: str, target: str, *, ondelete: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _sprints(metadata: MetaData) -> Table:
    name = "tasks_sprints"
    sprints = Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("name", Text, nullable=False),
        Column("goal", Text, nullable=False, server_default=""),
        Column("starts_on", Date),
        Column("ends_on", Date),
        Column("state", Text, nullable=False, server_default="planned"),
        Column("created_by", BigInteger, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("closed_at", DateTime(timezone=True)),
        Column("activated_at", DateTime(timezone=True)),
        _key(name, "project_id", "projects", ondelete="RESTRICT"),
        _key(name, "created_by", "users", ondelete="RESTRICT"),
        PrimaryKeyConstraint("id", name="tasks_sprints_pkey"),
        UniqueConstraint("project_id", "name", name="tasks_sprints_name_key"),
    )
    active = sprints.c.state == "active"
    Index("tasks_sprints_active_key", sprints.c.project_id, unique=True, postgresql_where=active)
    return sprints


def _items(metadata: MetaData) -> Table:
    name = "tasks_items"
    return Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("number", Integer, nullable=False),
        Column("type", Text, nullable=False, server_default="task"),
        Column("title", Text, nullable=False),
        Column("description_md", Text, nullable=False, server_default=""),
        Column("outcome_md", Text, nullable=False, server_default=""),
        Column("status", Text, nullable=False),
        Column("priority", Text, nullable=False, server_default="medium"),
        Column("due_on", Date),
        Column("sprint_id", BigInteger),
        Column("parent_id", BigInteger),
        Column("label", JSONB, nullable=False),
        Column("external_key", Text),
        Column("external_ref", JSONB),
        Column("created_by", BigInteger, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        _key(name, "project_id", "projects", ondelete="RESTRICT"),
        _key(name, "sprint_id", "tasks_sprints", ondelete="SET NULL"),
        _key(name, "parent_id", "tasks_items", ondelete="SET NULL"),
        _key(name, "created_by", "users", ondelete="RESTRICT"),
        PrimaryKeyConstraint("id", name="tasks_items_pkey"),
        UniqueConstraint("project_id", "number", name="tasks_items_number_key"),
        UniqueConstraint("project_id", "external_key", name="tasks_items_external_key"),
        Index("tasks_items_status_idx", "project_id", "status"),
        Index("tasks_items_sprint_idx", "sprint_id"),
    )


def _people(metadata: MetaData) -> Table:
    name = "tasks_people"
    return Table(
        name,
        metadata,
        Column("task_id", BigInteger, nullable=False),
        Column("role", Text, nullable=False),
        Column("user_id", BigInteger, nullable=False),
        _key(name, "task_id", "tasks_items", ondelete="CASCADE"),
        _key(name, "user_id", "users", ondelete="RESTRICT"),
        PrimaryKeyConstraint("task_id", "role", "user_id", name="tasks_people_pkey"),
        Index("tasks_people_user_idx", "user_id"),
    )


def _links(metadata: MetaData) -> Table:
    name = "tasks_links"
    return Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("task_id", BigInteger, nullable=False),
        Column("kind", Text, nullable=False),
        Column("srs_item_id", BigInteger),
        Column("plan_id", Text),
        Column("step_id", Text),
        Column("run_id", BigInteger),
        Column("url", Text),
        Column("created_by", BigInteger, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        _key(name, "task_id", "tasks_items", ondelete="CASCADE"),
        _key(name, "srs_item_id", "srs_items", ondelete="CASCADE"),
        _key(name, "run_id", "runs", ondelete="CASCADE"),
        _key(name, "created_by", "users", ondelete="RESTRICT"),
        PrimaryKeyConstraint("id", name="tasks_links_pkey"),
        Index("tasks_links_task_idx", "task_id"),
        Index("tasks_links_step_idx", "plan_id", "step_id"),
        Index("tasks_links_srs_item_idx", "srs_item_id"),
    )


def _events(metadata: MetaData) -> Table:
    name = "tasks_events"
    return Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("task_id", BigInteger, nullable=False),
        Column("actor_id", BigInteger),
        Column("field", Text, nullable=False),
        Column("from_value", Text),
        Column("to_value", Text),
        Column("cause", Text, nullable=False, server_default=""),
        Column("delivery_id", BigInteger),
        Column("at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        _key(name, "task_id", "tasks_items", ondelete="CASCADE"),
        _key(name, "actor_id", "users", ondelete="RESTRICT"),
        PrimaryKeyConstraint("id", name="tasks_events_pkey"),
        UniqueConstraint("task_id", "delivery_id", "field", name="tasks_events_delivery_key"),
        Index("tasks_events_task_idx", "task_id", "id"),
    )


def _comments(metadata: MetaData) -> Table:
    name = "tasks_comments"
    return Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("task_id", BigInteger, nullable=False),
        Column("author_id", BigInteger, nullable=False),
        Column("body", Text, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        _key(name, "task_id", "tasks_items", ondelete="CASCADE"),
        _key(name, "author_id", "users", ondelete="RESTRICT"),
        PrimaryKeyConstraint("id", name="tasks_comments_pkey"),
        Index("tasks_comments_task_idx", "task_id", "id"),
    )


def _blockers(metadata: MetaData) -> Table:
    name = "tasks_blockers"
    blockers = Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("task_id", BigInteger, nullable=False),
        Column("reason", Text, nullable=False),
        Column("waiting_user_id", BigInteger),
        Column("waiting_task_id", BigInteger),
        Column("waiting_text", Text),
        Column("since", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("created_by", BigInteger, nullable=False),
        Column("cleared_at", DateTime(timezone=True)),
        Column("cleared_by", BigInteger),
        _key(name, "task_id", "tasks_items", ondelete="CASCADE"),
        _key(name, "waiting_user_id", "users", ondelete="RESTRICT"),
        _key(name, "waiting_task_id", "tasks_items", ondelete="CASCADE"),
        _key(name, "created_by", "users", ondelete="RESTRICT"),
        _key(name, "cleared_by", "users", ondelete="RESTRICT"),
        PrimaryKeyConstraint("id", name="tasks_blockers_pkey"),
    )
    c = blockers.c
    blockers.append_constraint(
        CheckConstraint(
            func.num_nonnulls(c.waiting_user_id, c.waiting_task_id, c.waiting_text) == 1,
            name="tasks_blockers_waiting_check",
        )
    )
    blockers.append_constraint(
        CheckConstraint(
            or_(c.waiting_task_id.is_(None), c.waiting_task_id != c.task_id), name="tasks_blockers_self_check"
        )
    )
    reason = func.char_length(c.reason).between(1, MAX_REASON_CHARS)
    blockers.append_constraint(CheckConstraint(reason, name="tasks_blockers_reason_check"))
    cleared = c.cleared_at.is_(None) == c.cleared_by.is_(None)
    blockers.append_constraint(CheckConstraint(cleared, name="tasks_blockers_cleared_check"))
    Index("tasks_blockers_task_idx", c.task_id, c.id)
    Index("tasks_blockers_open_idx", c.task_id, postgresql_where=c.cleared_at.is_(None))
    return blockers


def tasks_tables(metadata: MetaData) -> TasksTables:
    """The tables of the core plugin tasks, added to ``metadata`` (which holds projects, users, runs and the tables of
    the plugin srs already)."""
    return TasksTables(
        sprints=_sprints(metadata),
        items=_items(metadata),
        people=_people(metadata),
        links=_links(metadata),
        events=_events(metadata),
        comments=_comments(metadata),
        blockers=_blockers(metadata),
    )
