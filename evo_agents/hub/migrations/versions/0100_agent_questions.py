"""The questions core plugins ask their agent types: agent_questions, the run kind question, the event agent.answer.

Revision ID: 0100
Revises: 0099
Create Date: 2026-10-22

The names below are copies of ``evo_agents.hub.runs``, ``evo_agents.hub.question_runs`` and
``evo_agents.hub.plugin_events`` as of this revision, never imports, so a later change to them takes a new revision.
Every statement is an Alembic operation on SQLAlchemy Core expressions.

agent_questions holds each question a core plugin asked an agent type of its own in a project (plan
core-plugin-agent-questions): the plugin, the project, the type, the run that answers it, the plugin's key of it
(``ref``, one per plugin and project), who asked as the plugin names them (``asker``), the clearance of the place the
answer goes (``cleared``), and once the run ended its outcome, the answer (only when done, at most MAX_ANSWER_CHARS)
and the label it carries.

runs takes the kind ``question``: one read-only turn of the type's agent, with the question in ``request`` like a task
run's, an agent type like a task run's, no plan, and the repos of the project, none when it has none.
plugin_deliveries takes the kind ``agent.answer``, the event the plugin gets when a question run ends.

Going back deletes the deliveries of agent.answer, drops agent_questions, deletes the question runs, and puts the
checks of 0098 back.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0100"
down_revision = "0099"
branch_labels = None
depends_on = None

TABLE = "agent_questions"
KINDS_0098 = (  # runs.kind as 0067 set it; local: evo_agents.hub.runs.LOCAL_KIND
    "step",
    "plan",
    "review",
    "judge",
    "author",
    "chat",
    "local",
    "task",
    "replay",
    "history",
    "release",
)
KINDS = (*KINDS_0098, "question")  # evo_agents.hub.runs.SHOWN_KINDS
ANY_PLAN = ("author", "local", "judge")  # the kinds that may name a plan or none, as of 0081
NO_PLAN_0098 = ("review", "chat", "task", "history", "release")  # the kinds of run on no plan, as of 0081
NO_PLAN = (*NO_PLAN_0098, "question")
ASKED_0098 = ("author", "task")  # the kinds whose request is set, as of 0042
ASKED = (*ASKED_0098, "question")
ANY_REPOS_0098 = ("chat", "local", "task", "history")  # the kinds whose repos may be NULL, as of 0059
ANY_REPOS = (*ANY_REPOS_0098, "question")
TYPED_0098 = ("task",)  # the kinds that name an agent type, as of 0042
TYPED = (*TYPED_0098, "question")
DELIVERY_KINDS_0098 = (  # plugin_deliveries.kind as 0092 set it
    "run.state",
    "chat.message",
    "plan.step",
    "notification",
    "task.result",
    "canvas.claim",
    "canvas.baseline",
    "signal.posted",
    "srs.version.approved",
    "tasks.status",
)
ANSWER_EVENT = "agent.answer"  # evo_agents.hub.question_runs.ANSWER_EVENT
DELIVERY_KINDS = (*DELIVERY_KINDS_0098, ANSWER_EVENT)  # evo_agents.hub.plugin_events.KINDS
DELIVERY_STATES = ("pending", "delivered", "failed")
NAME_PATTERN = "^[a-z][a-z0-9_-]{0,62}$"
MAX_ERROR_CHARS = 2000
OUTCOMES = ("done", "failed", "cancelled", "refused")  # agent_questions.outcome
MAX_LINE_CHARS = 200  # question_runs.MAX_ASKER_CHARS, MAX_REF_CHARS
LINE = "^[^\\x01-\\x1f\\x7f]+$"  # one line without control characters (Postgres keeps no NUL)
MAX_ANSWER_CHARS = 8000  # question_runs.MAX_ANSWER_CHARS

col = sa.column


def _replace(table: str, name: str, rule) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, rule)


def _checks(kinds, no_plan, asked, any_repos, typed, delivery_kinds) -> None:
    """The checks that name the kinds of runs and of deliveries."""
    kind = col("kind")
    _replace("runs", "runs_kind_check", kind.in_(kinds))
    _replace("runs", "runs_kind_plan_check", sa.or_(kind.in_(ANY_PLAN), kind.in_(no_plan) == col("plan_id").is_(None)))
    _replace("runs", "runs_author_request_check", kind.in_(asked) == col("request").is_not(None))
    _replace("runs", "runs_kind_repos_check", sa.or_(kind.in_(any_repos), (kind == "step") == col("repos").is_(None)))
    _replace("runs", "runs_task_check", kind.in_(typed) == col("agent_type_id").is_not(None))
    _replace(
        "plugin_deliveries",
        "plugin_deliveries_check",
        sa.and_(
            kind.in_(delivery_kinds),
            col("contribution").regexp_match(NAME_PATTERN),
            col("state").in_(DELIVERY_STATES),
            col("attempts") >= 0,
            sa.func.char_length(col("last_error")) <= MAX_ERROR_CHARS,
            (col("state") == "delivered") == col("delivered_at").is_not(None),
        ),
    )


def _key(column: str, target: str):
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete="CASCADE", name=f"{TABLE}_{column}_fkey")


def _line(name: str):
    return sa.and_(sa.func.char_length(col(name)) <= MAX_LINE_CHARS, col(name).regexp_match(LINE))


def upgrade() -> None:
    _checks(KINDS, NO_PLAN, ASKED, ANY_REPOS, TYPED, DELIVERY_KINDS)
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("plugin_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("agent_type_id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column("asker", sa.Text(), nullable=False),
        sa.Column("cleared", postgresql.JSONB(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("label", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        _key("plugin_id", "plugins"),
        _key("project_id", "projects"),
        _key("agent_type_id", "project_agent_types"),
        _key("run_id", "runs"),
        sa.PrimaryKeyConstraint("id", name=f"{TABLE}_pkey"),
        sa.UniqueConstraint("run_id", name=f"{TABLE}_run_id_key"),
        sa.UniqueConstraint("plugin_id", "project_id", "ref", name=f"{TABLE}_ref_key"),
        sa.CheckConstraint(
            sa.and_(
                _line("ref"),
                _line("asker"),
                sa.func.jsonb_typeof(col("cleared")) == "object",
                sa.or_(col("outcome").is_(None), col("outcome").in_(OUTCOMES)),
                sa.or_(col("answer").is_(None), col("outcome").is_not_distinct_from("done")),
                sa.func.char_length(col("answer")) <= MAX_ANSWER_CHARS,
                col("outcome").is_(None) == col("answered_at").is_(None),
            ),
            name=f"{TABLE}_check",
        ),
    )


def downgrade() -> None:
    deliveries = sa.table("plugin_deliveries", col("kind"))
    op.execute(deliveries.delete().where(deliveries.c.kind == ANSWER_EVENT))
    op.drop_table(TABLE)
    runs = sa.table("runs", col("kind"))
    op.execute(runs.delete().where(runs.c.kind == "question"))
    _checks(KINDS_0098, NO_PLAN_0098, ASKED_0098, ANY_REPOS_0098, TYPED_0098, DELIVERY_KINDS_0098)
