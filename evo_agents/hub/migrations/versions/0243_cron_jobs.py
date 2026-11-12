"""The cron jobs of a project and their fires, and the cron job of a run.

Revision ID: 0243
Revises: 0242
Create Date: 2026-11-12

The names below are copies of ``evo_agents.hub.tables_cron`` as of this revision, never imports, so a later change to
them takes a new revision.

cron_jobs holds each cron job of a project (plan hub-cron-jobs, criterion a1): its name, unique in the project, five
cron fields and IANA time zone, the kind of work it starts (agent, plan or command) with what that kind needs, its
owner, its revision, and its pause (when, by whom, why). cron_fires holds each fire of a job, one per (job, scheduled
minute), with its outcome (started, busy, missed or refused; NULL until the hub handled it), why, and the run it
started. runs gain cron_job_id, the job whose fire queued the run. A project's jobs go with the project and a job's
fires with the job; a run or a person deleted under them leaves them, the link cleared, and an owner deleted takes
their jobs.

Going back drops the two tables and the column of runs.
"""

import sqlalchemy as sa
from alembic import op

revision = "0243"
down_revision = "0242"
branch_labels = None
depends_on = None

KINDS = ("agent", "plan", "command")
OUTCOMES = ("started", "busy", "missed", "refused")
NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"

col = sa.column


def _key(table: str, column: str, target: str, *, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _when(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def _jobs() -> None:
    name = "cron_jobs"
    # An agent job's profile_id may be cleared by a deletion under it: such a job is refused at its fires, not lost
    work = sa.or_(
        sa.and_(col("kind") == "agent", col("prompt").is_not(None)),
        sa.and_(col("kind") == "plan", col("plan_id").is_not(None)),
        sa.and_(col("kind") == "command", col("command").is_not(None), col("timeout_minutes").is_not(None)),
    )
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("cron", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("profile_id", sa.BigInteger(), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("plan_id", sa.Text(), nullable=True),
        sa.Column("command", sa.Text(), nullable=True),
        sa.Column("repo", sa.Text(), nullable=True),
        sa.Column("timeout_minutes", sa.Integer(), nullable=True),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        _stamp("created_at"),
        _stamp("updated_at"),
        _when("paused_at"),
        sa.Column("paused_by", sa.BigInteger(), nullable=True),
        sa.Column("paused_reason", sa.Text(), nullable=True),
        _key(name, "project_id", "projects", ondelete="CASCADE"),
        _key(name, "profile_id", "agent_profiles", ondelete="SET NULL"),
        _key(name, "chat_id", "chats", ondelete="SET NULL"),
        _key(name, "owner_id", "users", ondelete="CASCADE"),
        _key(name, "updated_by", "users", ondelete="SET NULL"),
        _key(name, "paused_by", "users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=f"{name}_pkey"),
        sa.UniqueConstraint("project_id", "name", name=f"{name}_project_id_name_key"),
        sa.CheckConstraint(col("kind").in_(KINDS), name=f"{name}_kind_check"),
        sa.CheckConstraint(col("name").regexp_match(NAME_PATTERN), name=f"{name}_name_check"),
        sa.CheckConstraint(work, name=f"{name}_work_check"),
        sa.CheckConstraint(
            sa.or_(col("timeout_minutes").is_(None), col("timeout_minutes").between(1, 240)),
            name=f"{name}_timeout_minutes_check",
        ),
        sa.CheckConstraint(col("revision") >= 1, name=f"{name}_revision_check"),
    )


def _fires() -> None:
    name = "cron_fires"
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("job_id", sa.BigInteger(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("manual", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        _stamp("created_at"),
        _when("handled_at"),
        _key(name, "job_id", "cron_jobs", ondelete="CASCADE"),
        _key(name, "actor_id", "users", ondelete="SET NULL"),
        _key(name, "run_id", "runs", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=f"{name}_pkey"),
        sa.UniqueConstraint("job_id", "scheduled_for", name=f"{name}_job_id_scheduled_for_key"),
        sa.CheckConstraint(
            sa.or_(col("outcome").is_(None), col("outcome").in_(OUTCOMES)), name=f"{name}_outcome_check"
        ),
    )
    op.create_index(f"{name}_pending_idx", name, ["id"], postgresql_where=col("outcome").is_(None))


def upgrade() -> None:
    _jobs()
    _fires()
    op.add_column("runs", sa.Column("cron_job_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key("runs_cron_job_id_fkey", "runs", "cron_jobs", ["cron_job_id"], ["id"], ondelete="SET NULL")
    op.create_index(
        "runs_cron_job_idx",
        "runs",
        ["cron_job_id", col("id").desc()],
        postgresql_where=col("cron_job_id").is_not(None),
    )


def downgrade() -> None:
    op.drop_index("runs_cron_job_idx", table_name="runs")
    op.drop_constraint("runs_cron_job_id_fkey", "runs", type_="foreignkey")
    op.drop_column("runs", "cron_job_id")
    op.drop_table("cron_fires")
    op.drop_table("cron_jobs")
