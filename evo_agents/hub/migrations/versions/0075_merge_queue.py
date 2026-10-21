"""The hub's merge queue on GitLab: one row for each merge request it takes, and its ledger.

Revision ID: 0075
Revises: 0074
Create Date: 2026-10-21

The names below are copies of ``evo_agents.hub.tables_merge`` and ``evo_agents.hub.merge_gate`` as of this revision,
never imports, so a later change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy
Core expressions.

merge_queue holds each merge request the queue took, or that the hub opened for a run of a project whose autonomy
policy has its repo in mode auto (plan gitlab-merge-queue, criterion a4): its repo and iid (null until the hub opened
it), where it came from (SOURCES) and where it stands (STATES), why, what the gates read and decided, and the judge run
in flight with the SHA-256 of its own key. A plan has one merge request in each repo (merge_queue_plan_key).
merge_events is its ledger, only ever added to: each gate with whether it passed and why, each verdict, and what the
queue does later (EVENT_KINDS). Both go with their project.

A judge run of the queue runs on no plan and no schedule: the merge request it judges names it (merge_queue.
judge_run_id), and the policy's admin dispatches it, not a night shift. runs_kind_plan_check lets a judge run name no
plan, and runs_judge_schedule_check, which held every judge run to a schedule, now holds a judge run to one exactly
when it is on a plan, the Curator's. No column of runs changes.

Going back drops both tables, deletes the judge runs on no plan and puts back the checks of 0067 and 0017: the hub of
0074 has no queue, and its projects' merge requests stay open for a person.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None

SOURCES = ("plan", "task", "builder", "label")  # evo_agents.hub.merge_gate.SOURCES
STATES = (  # evo_agents.hub.merge_gate.STATES
    "gating",
    "judging",
    "ready",
    "rebasing",
    "testing",
    "merging",
    "merged",
    "verifying",
    "releasing",
    "done",
    "rolling_back",
    "reverting",
    "reverted",
    "blocked",
    "paused",
    "closed",
)
EVENT_KINDS = (  # evo_agents.hub.merge_gate.EVENT_KINDS
    "opened",
    "queued",
    "head",
    "gate",
    "judged",
    "rebased",
    "pipeline",
    "merged",
    "uat_check",
    "prod_deploy",
    "prod_check",
    "retried",
    "rollback",
    "revert_opened",
    "reverted",
    "sampled",
    "sample_answered",
    "paused",
    "closed",
)
LAST_GATE = 7  # gates 0 to 7, evo_agents.hub.merge_gate.GATES
MAX_REASON_CHARS = 2000  # evo_agents.hub.merge_gate.MAX_REASON_CHARS
MAX_DESCRIPTION_BYTES = 64 * 1024  # evo_agents.hub.merge_gate.MAX_KEPT_DESCRIPTION_BYTES
NO_PLAN = ("review", "chat", "task", "history", "release")  # the kinds of run on no plan, as of 0067
ANY_PLAN_0067 = ("author", "local")  # the kinds that may name a plan or none, as of 0067
ANY_PLAN = (*ANY_PLAN_0067, "judge")

col = sa.column


def _runs_checks(any_plan, judge_rule) -> None:
    kind = col("kind")
    for name, rule in (
        ("runs_kind_plan_check", sa.or_(kind.in_(any_plan), kind.in_(NO_PLAN) == col("plan_id").is_(None))),
        ("runs_judge_schedule_check", sa.or_(kind != "judge", judge_rule)),
    ):
        op.drop_constraint(name, "runs", type_="check")
        op.create_check_constraint(name, "runs", rule)


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _when(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def _json(name: str, default: str | None = None) -> sa.Column:
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), server_default=default, nullable=default is None)


def upgrade() -> None:
    op.create_table(
        "merge_queue",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("iid", sa.Integer(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("target_branch", sa.Text(), nullable=False),
        sa.Column("source_branch", sa.Text(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("plan_id", sa.Text(), nullable=True),
        _json("steps", "[]"),
        sa.Column("change_id", sa.BigInteger(), nullable=True),
        sa.Column("profile", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("web_url", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), server_default="gating", nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("head_sha", sa.Text(), nullable=True),
        _when("head_at"),
        sa.Column("fingerprint", sa.Text(), nullable=True),
        _json("verdict"),
        _json("risk"),
        sa.Column("judge_run_id", sa.BigInteger(), nullable=True),
        sa.Column("judge_key", sa.Text(), nullable=True),
        sa.Column("judge_attempts", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("commented", sa.Text(), nullable=True),
        _when("queued_at"),
        _when("merged_at"),
        sa.Column("before_sha", sa.Text(), nullable=True),
        sa.Column("after_sha", sa.Text(), nullable=True),
        _stamp("created_at"),
        _stamp("updated_at"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="merge_queue_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL", name="merge_queue_run_id_fkey"),
        sa.ForeignKeyConstraint(
            ["change_id"], ["curator_changes.id"], ondelete="SET NULL", name="merge_queue_change_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["judge_run_id"], ["runs.id"], ondelete="SET NULL", name="merge_queue_judge_run_id_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="merge_queue_pkey"),
        sa.UniqueConstraint("project_id", "repo", "iid", name="merge_queue_project_id_repo_iid_key"),
        sa.CheckConstraint(
            sa.and_(
                col("source").in_(SOURCES),
                col("state").in_(STATES),
                col("iid") >= 1,
                col("judge_attempts") >= 0,
                sa.func.char_length(col("reason")) <= MAX_REASON_CHARS,
                sa.func.octet_length(col("description")) <= MAX_DESCRIPTION_BYTES,
                sa.func.jsonb_typeof(col("steps", postgresql.JSONB)) == "array",
                sa.or_(col("source") != "plan", col("plan_id").is_not(None)),
            ),
            name="merge_queue_check",
        ),
    )
    op.create_index("merge_queue_state_idx", "merge_queue", ["project_id", "state", "id"])
    op.create_index("merge_queue_judge_run_idx", "merge_queue", ["judge_run_id"])
    op.create_index("merge_queue_run_idx", "merge_queue", ["run_id"])
    op.create_index(
        "merge_queue_plan_key",
        "merge_queue",
        ["project_id", "repo", "plan_id"],
        unique=True,
        postgresql_where=col("source") == "plan",
    )
    op.create_table(
        "merge_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("queue_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("gate", sa.SmallInteger(), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("sha", sa.Text(), nullable=True),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        _json("detail", "{}"),
        _stamp("created_at"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="merge_events_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["queue_id"], ["merge_queue.id"], ondelete="CASCADE", name="merge_events_queue_id_fkey"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL", name="merge_events_run_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="merge_events_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("kind").in_(EVENT_KINDS),
                col("gate").between(0, LAST_GATE),
                (col("kind") == "gate") == col("gate").is_not(None),
                sa.func.char_length(col("reason")) <= MAX_REASON_CHARS,
                sa.func.jsonb_typeof(col("detail", postgresql.JSONB)) == "object",
            ),
            name="merge_events_check",
        ),
    )
    op.create_index("merge_events_queue_idx", "merge_events", ["queue_id", "id"])
    op.create_index("merge_events_project_idx", "merge_events", ["project_id", "id"])
    _runs_checks(ANY_PLAN, col("schedule_id").is_not(None) == col("plan_id").is_not(None))


def downgrade() -> None:
    op.drop_table("merge_events")
    op.drop_table("merge_queue")
    runs = sa.table("runs", col("kind"), col("plan_id"))
    op.execute(runs.delete().where(runs.c.kind == "judge", runs.c.plan_id.is_(None)))
    _runs_checks(ANY_PLAN_0067, col("schedule_id").is_not(None))
