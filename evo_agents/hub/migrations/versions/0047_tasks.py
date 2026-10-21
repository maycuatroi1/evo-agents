"""The tables of the core plugin tasks: tasks with their sprints, people, links, history and comments.

Revision ID: 0047
Revises: 0046
Create Date: 2026-10-19

The names below are copies of ``evo_agents.hub.tables_tasks`` as of this revision, never imports, so a later change
to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

The plugin is ``evo_agents.core_plugins.tasks``; every table carries its prefix. tasks_sprints holds a project's
sprints, at most one of them active; tasks_items the tasks, numbered within the project, each with the label it
carries and, when an import made it, the key of its source, unique in the project; tasks_people the assignees and the
BA, QA and Dev of each task; tasks_links what a task links to: one item of the plugin srs, one step of a plan, one run
or one pull request, and only the columns of its kind; tasks_events the history of each task, an event of a plugin
delivery written once per task and field; tasks_comments the comments on a task.

Going back drops the six tables.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None

SPRINT_STATES = ("planned", "active", "closed")
PRIORITIES = ("urgent", "high", "medium", "low")
PEOPLE_ROLES = ("assignee", "ba", "qa", "dev")
KEY = r"^[a-z][a-z0-9_]{0,39}$"
MAX_TITLE_CHARS = 300
MAX_DESCRIPTION_CHARS = 100_000
MAX_SPRINT_NAME_CHARS = 100
MAX_GOAL_CHARS = 2_000
MAX_URL_CHARS = 2_000
MAX_EXTERNAL_KEY_CHARS = 300
MAX_PLAN_ID_CHARS = 200
MAX_STEP_CHARS = 100
MAX_FIELD_CHARS = 40
MAX_VALUE_CHARS = 1_000
MAX_CAUSE_CHARS = 500
MAX_COMMENT_CHARS = 20_000

col = sa.column
TABLES = (  # in the order going back drops them
    "tasks_comments",
    "tasks_events",
    "tasks_links",
    "tasks_people",
    "tasks_items",
    "tasks_sprints",
)
INDEXES = (  # (index, table), dropped before the tables
    ("tasks_comments_task_idx", "tasks_comments"),
    ("tasks_events_task_idx", "tasks_events"),
    ("tasks_links_srs_item_idx", "tasks_links"),
    ("tasks_links_step_idx", "tasks_links"),
    ("tasks_links_task_idx", "tasks_links"),
    ("tasks_people_user_idx", "tasks_people"),
    ("tasks_items_sprint_idx", "tasks_items"),
    ("tasks_items_status_idx", "tasks_items"),
    ("tasks_sprints_active_key", "tasks_sprints"),
)


def _key(table: str, column: str, target: str, *, ondelete: str):
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _length(name: str, low: int, high: int):
    return sa.func.char_length(col(name)).between(low, high)


def _stamp(name: str):
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _sprints() -> None:
    name, state = "tasks_sprints", col("state")
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("goal", sa.Text(), server_default="", nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=True),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column("state", sa.Text(), server_default="planned", nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        _stamp("created_at"),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        _key(name, "project_id", "projects", ondelete="RESTRICT"),
        _key(name, "created_by", "users", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="tasks_sprints_pkey"),
        sa.UniqueConstraint("project_id", "name", name="tasks_sprints_name_key"),
        sa.CheckConstraint(
            sa.and_(
                _length("name", 1, MAX_SPRINT_NAME_CHARS),
                sa.func.char_length(col("goal")) <= MAX_GOAL_CHARS,
                state.in_(SPRINT_STATES),
                col("ends_on") >= col("starts_on"),
                (state == "closed") == col("closed_at").is_not(None),
            ),
            name="tasks_sprints_check",
        ),
    )
    op.create_index("tasks_sprints_active_key", name, ["project_id"], unique=True, postgresql_where=state == "active")


def _items() -> None:
    name = "tasks_items"
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("type", sa.Text(), server_default="task", nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description_md", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("priority", sa.Text(), server_default="medium", nullable=False),
        sa.Column("due_on", sa.Date(), nullable=True),
        sa.Column("sprint_id", sa.BigInteger(), nullable=True),
        sa.Column("parent_id", sa.BigInteger(), nullable=True),
        sa.Column("label", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("external_key", sa.Text(), nullable=True),
        sa.Column("external_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        _stamp("created_at"),
        _stamp("updated_at"),
        _key(name, "project_id", "projects", ondelete="RESTRICT"),
        _key(name, "sprint_id", "tasks_sprints", ondelete="SET NULL"),
        _key(name, "parent_id", "tasks_items", ondelete="SET NULL"),
        _key(name, "created_by", "users", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="tasks_items_pkey"),
        sa.UniqueConstraint("project_id", "number", name="tasks_items_number_key"),
        sa.UniqueConstraint("project_id", "external_key", name="tasks_items_external_key"),
        sa.CheckConstraint(
            sa.and_(
                col("number") >= 1,
                col("type").regexp_match(KEY),
                _length("title", 1, MAX_TITLE_CHARS),
                sa.func.char_length(col("description_md")) <= MAX_DESCRIPTION_CHARS,
                col("status").regexp_match(KEY),
                col("priority").in_(PRIORITIES),
                _length("external_key", 1, MAX_EXTERNAL_KEY_CHARS),
                col("parent_id") != col("id"),
            ),
            name="tasks_items_check",
        ),
    )
    op.create_index("tasks_items_status_idx", name, ["project_id", "status"])
    op.create_index("tasks_items_sprint_idx", name, ["sprint_id"])


def _people() -> None:
    name = "tasks_people"
    op.create_table(
        name,
        sa.Column("task_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        _key(name, "task_id", "tasks_items", ondelete="CASCADE"),
        _key(name, "user_id", "users", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("task_id", "role", "user_id", name="tasks_people_pkey"),
        sa.CheckConstraint(col("role").in_(PEOPLE_ROLES), name="tasks_people_check"),
    )
    op.create_index("tasks_people_user_idx", name, ["user_id"])


def _only(kind: str, *present: str):
    """A link of ``kind`` sets the columns ``present`` and leaves the other target columns NULL."""
    targets = ("srs_item_id", "plan_id", "step_id", "run_id", "url")
    held = [col(name).is_not(None) if name in present else col(name).is_(None) for name in targets]
    return sa.and_(col("kind") == kind, *held)


def _links() -> None:
    name = "tasks_links"
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("task_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("srs_item_id", sa.BigInteger(), nullable=True),
        sa.Column("plan_id", sa.Text(), nullable=True),
        sa.Column("step_id", sa.Text(), nullable=True),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        _stamp("created_at"),
        _key(name, "task_id", "tasks_items", ondelete="CASCADE"),
        _key(name, "srs_item_id", "srs_items", ondelete="CASCADE"),
        _key(name, "run_id", "runs", ondelete="CASCADE"),
        _key(name, "created_by", "users", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="tasks_links_pkey"),
        sa.CheckConstraint(
            sa.and_(
                sa.or_(
                    _only("srs_item", "srs_item_id"),
                    _only("plan_step", "plan_id", "step_id"),
                    _only("run", "run_id"),
                    _only("pull", "url"),
                ),
                _length("plan_id", 1, MAX_PLAN_ID_CHARS),
                _length("step_id", 1, MAX_STEP_CHARS),
                _length("url", 1, MAX_URL_CHARS),
            ),
            name="tasks_links_check",
        ),
    )
    op.create_index("tasks_links_task_idx", name, ["task_id"])
    op.create_index("tasks_links_step_idx", name, ["plan_id", "step_id"])
    op.create_index("tasks_links_srs_item_idx", name, ["srs_item_id"])


def _events() -> None:
    name = "tasks_events"
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("task_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("from_value", sa.Text(), nullable=True),
        sa.Column("to_value", sa.Text(), nullable=True),
        sa.Column("cause", sa.Text(), server_default="", nullable=False),
        sa.Column("delivery_id", sa.BigInteger(), nullable=True),
        _stamp("at"),
        _key(name, "task_id", "tasks_items", ondelete="CASCADE"),
        _key(name, "actor_id", "users", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="tasks_events_pkey"),
        sa.UniqueConstraint("task_id", "delivery_id", "field", name="tasks_events_delivery_key"),
        sa.CheckConstraint(
            sa.and_(
                _length("field", 1, MAX_FIELD_CHARS),
                sa.func.char_length(col("from_value")) <= MAX_VALUE_CHARS,
                sa.func.char_length(col("to_value")) <= MAX_VALUE_CHARS,
                sa.func.char_length(col("cause")) <= MAX_CAUSE_CHARS,
            ),
            name="tasks_events_check",
        ),
    )
    op.create_index("tasks_events_task_idx", name, ["task_id", "id"])


def _comments() -> None:
    name = "tasks_comments"
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("task_id", sa.BigInteger(), nullable=False),
        sa.Column("author_id", sa.BigInteger(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        _stamp("created_at"),
        _key(name, "task_id", "tasks_items", ondelete="CASCADE"),
        _key(name, "author_id", "users", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="tasks_comments_pkey"),
        sa.CheckConstraint(_length("body", 1, MAX_COMMENT_CHARS), name="tasks_comments_check"),
    )
    op.create_index("tasks_comments_task_idx", name, ["task_id", "id"])


def upgrade() -> None:
    _sprints()
    _items()
    _people()
    _links()
    _events()
    _comments()


def downgrade() -> None:
    for index, table in INDEXES:
        op.drop_index(index, table_name=table)
    for name in TABLES:
        op.drop_table(name)
