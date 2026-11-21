"""Member task runs and outbound webhooks: a task run with no agent type, its input, and a project's webhooks.

Revision ID: 0263
Revises: 0262
Create Date: 2026-11-21

Plan evo-creative-workspace, step 2, criterion a2. A member who writes to a project hands a task run of their own at
POST /v1/projects/{p}/task-runs (``evo_agents.hub.server.member_tasks``): a run of kind ``task`` that names no agent
type, so runs_task_check lets a task run name one or not, and still makes every other kind of TYPED name one exactly
when it is of TYPED. member_tasks keeps the run's JSON input and its assets' URIs. project_webhooks holds the outbound
webhooks a project's admin sets, and webhook_deliveries is their outbox (``evo_agents.hub.webhook_events``). The kinds
below are copies, never imports, so a later change takes a new revision.

The tables start empty. Going back deletes the task runs with no agent type, retries first, drops the three tables
and puts runs_task_check back as 0100 left it.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0263"
down_revision = "0262"
branch_labels = None
depends_on = None

TYPED_0100 = ("task", "question")  # the kinds that name an agent type, as of 0100
MAX_ASSETS = 20  # evo_agents.hub.server.member_tasks.MAX_ASSETS
EVENTS = ("run.state", "task.result", "chat.message", "curator.proposal")  # evo_agents.hub.webhook_events.EVENTS
STATES = ("pending", "delivered", "failed")  # evo_agents.hub.webhook_events.STATES
MAX_URL_CHARS = 2048  # evo_agents.hub.webhook_events.MAX_URL_CHARS
MAX_ERROR_CHARS = 2000  # evo_agents.hub.webhook_events.MAX_ERROR_CHARS
MAX_ATTEMPTS_RUN = 10  # more attempts than any run has: retries are deleted before the run they retry

col = sa.column


def _task_check(rule) -> None:
    op.drop_constraint("runs_task_check", "runs", type_="check")
    op.create_check_constraint("runs_task_check", "runs", rule)


def _typed():
    return col("kind").in_(TYPED_0100) == col("agent_type_id").is_not(None)


def _texts(name: str):
    return sa.column(name, postgresql.ARRAY(sa.Text()))


def upgrade() -> None:
    _task_check(sa.or_(sa.and_(col("kind") == "task", col("agent_type_id").is_(None)), _typed()))
    op.create_table(
        "member_tasks",
        sa.Column("task_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("input", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("assets", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("task_id", name="member_tasks_pkey"),
        sa.ForeignKeyConstraint(["task_id"], ["runs.id"], ondelete="CASCADE", name="member_tasks_task_id_fkey"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="member_tasks_project_id_fkey"
        ),
        sa.CheckConstraint(sa.func.jsonb_typeof(col("input")) == "object", name="member_tasks_input_check"),
        sa.CheckConstraint(
            sa.case(
                (
                    sa.func.jsonb_typeof(col("assets")) == "array",
                    sa.func.jsonb_array_length(col("assets")) <= MAX_ASSETS,
                ),
                else_=sa.false(),
            ),
            name="member_tasks_assets_check",
        ),
    )
    op.create_index("member_tasks_project_id_idx", "member_tasks", ["project_id"])
    op.create_table(
        "project_webhooks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("events", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("sealed_secret", postgresql.JSONB(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="project_webhooks_pkey"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="project_webhooks_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL", name="project_webhooks_created_by_fkey"
        ),
        sa.CheckConstraint(
            sa.and_(col("url").startswith("https://"), sa.func.char_length(col("url")) <= MAX_URL_CHARS),
            name="project_webhooks_url_check",
        ),
        sa.CheckConstraint(
            sa.and_(
                sa.func.cardinality(_texts("events")) >= 1,
                _texts("events").contained_by(postgresql.array(EVENTS, type_=sa.Text())),
            ),
            name="project_webhooks_events_check",
        ),
    )
    op.create_index("project_webhooks_project_id_idx", "project_webhooks", ["project_id"])
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("webhook_id", sa.BigInteger(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.Text(), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_status", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="webhook_deliveries_pkey"),
        sa.ForeignKeyConstraint(
            ["webhook_id"], ["project_webhooks.id"], ondelete="CASCADE", name="webhook_deliveries_webhook_id_fkey"
        ),
        sa.CheckConstraint(
            sa.and_(
                col("event").in_(EVENTS),
                col("state").in_(STATES),
                col("attempts") >= 0,
                sa.func.char_length(col("last_error")) <= MAX_ERROR_CHARS,
                (col("state") == "delivered") == col("delivered_at").is_not(None),
            ),
            name="webhook_deliveries_check",
        ),
    )
    op.create_index("webhook_deliveries_due_idx", "webhook_deliveries", ["state", "next_at"])
    op.create_index("webhook_deliveries_webhook_id_idx", "webhook_deliveries", ["webhook_id"])


def downgrade() -> None:
    op.drop_index("webhook_deliveries_webhook_id_idx", table_name="webhook_deliveries")
    op.drop_index("webhook_deliveries_due_idx", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_index("project_webhooks_project_id_idx", table_name="project_webhooks")
    op.drop_table("project_webhooks")
    op.drop_index("member_tasks_project_id_idx", table_name="member_tasks")
    op.drop_table("member_tasks")
    runs = sa.table("runs", col("kind"), col("agent_type_id"), col("attempt"))
    untyped = sa.and_(runs.c.kind == "task", runs.c.agent_type_id.is_(None))
    for attempt in range(MAX_ATTEMPTS_RUN, 1, -1):  # a retry names the attempt before it: delete the retries first
        op.execute(runs.delete().where(untyped, runs.c.attempt == attempt))
    op.execute(runs.delete().where(untyped))
    _task_check(_typed())
