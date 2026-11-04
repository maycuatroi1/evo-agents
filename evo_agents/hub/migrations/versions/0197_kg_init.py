"""The kg_init run of the Curator, and its lens.

Revision ID: 0197
Revises: 0196
Create Date: 2026-11-04

kg_init_runs is new (plan kg-data-profile-curator, criterion a5): one row each time the hub has the Curator configure a
project's knowledge graph from its data profile (``evo_agents.hub.server.kg_init``). ``via`` says what asked for it: the
first successful build of a project with a knowledge config (first_build), a profile that changed a lot since the last
run's (profile), or a call (manual). A row holds the reasons, the build and the profile the run was given, the review
run of the agent it queued (lens kg_init), what it was shown and decided (``body``) and its report. One run of a
project is in flight at a time (``kg_init_runs_active_key``), so a second call finds the first. The table starts empty:
no existing project gets a run until a writer calls for one.

findings.lens and proposals.lens take ``kg_init``: the values below are copies of ``evo_agents.hub.review.LENSES`` as of
this revision, never imports, so a later change to them takes a new revision.

runs_review_schedule_check (0014) said a review run names the schedule that queued it. A kg_init run needs no night
shift: the hub queues it, with the credential ``machine``, on the project's default agent pool. The check now also lets a
review run of that credential have no schedule; every row that passed the old check passes the new one. No column of
another table changes.

Going back drops the table, deletes the findings and the proposals of the lens (and those dropped as repeats of one),
deletes the review runs that name no schedule, and puts the checks of 0094 and 0014 back.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0197"
down_revision = "0196"
branch_labels = None
depends_on = None

TABLE = "kg_init_runs"
ACTIVE = ("queued", "running")  # a copy of evo_agents.hub.tables_kg.INIT_ACTIVE
STATES = (*ACTIVE, "done", "failed")
VIAS = ("first_build", "profile", "manual")
LENSES_0094 = (
    "tool_errors",
    "environment",
    "corrections",
    "failed_runs",
    "tech_debt",
    "code_health",
    "docs_drift",
    "skills_memory",
    "cost",
    "security",
    "product_goals",
    "kg_health",
    "runtime",
    "harness",
    "product",
)
NEW_LENSES = ("kg_init",)
LENSES = (*LENSES_0094, *NEW_LENSES)

REVIEW_CHECK = "runs_review_schedule_check"

col = sa.column


def _key(column: str, target: str, *, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{TABLE}_{column}_fkey")


def _lenses(lenses) -> None:
    for table in ("findings", "proposals"):
        op.drop_constraint(f"{table}_lens_check", table, type_="check")
        op.create_check_constraint(f"{table}_lens_check", table, col("lens").in_(lenses))


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("via", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.BigInteger(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("why", postgresql.JSONB(), nullable=False),
        sa.Column("build_id", sa.BigInteger(), nullable=True),
        sa.Column("profile", postgresql.JSONB(), nullable=True),
        sa.Column("agent_run_id", sa.BigInteger(), nullable=True),
        sa.Column("body", postgresql.JSONB(), nullable=False),
        sa.Column("report", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        _key("project_id", "projects", ondelete="CASCADE"),
        _key("requested_by", "users", ondelete="SET NULL"),
        _key("agent_run_id", "runs", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=f"{TABLE}_pkey"),
        sa.CheckConstraint(col("via").in_(VIAS), name=f"{TABLE}_via_check"),
        sa.CheckConstraint(col("state").in_(STATES), name=f"{TABLE}_state_check"),
    )
    op.create_index(f"{TABLE}_project_idx", TABLE, ["project_id", "id"])
    op.create_index(f"{TABLE}_agent_run_idx", TABLE, ["agent_run_id"])
    op.create_index(
        f"{TABLE}_active_key",
        TABLE,
        ["project_id"],
        unique=True,
        postgresql_where=col("state").in_(ACTIVE),
    )
    _lenses(LENSES)
    op.drop_constraint(REVIEW_CHECK, "runs", type_="check")
    op.create_check_constraint(
        REVIEW_CHECK,
        "runs",
        sa.or_(col("kind") != "review", col("schedule_id").is_not(None), col("dispatched_via") == "machine"),
    )


def downgrade() -> None:
    findings = sa.table("findings", col("lens"))
    proposals = sa.table("proposals", col("id"), col("lens"), col("duplicate_of"))
    new = sa.select(proposals.c.id).where(proposals.c.lens.in_(NEW_LENSES))
    op.execute(proposals.delete().where(proposals.c.duplicate_of.in_(new)))  # dropped as repeats of one
    op.execute(proposals.delete().where(proposals.c.lens.in_(NEW_LENSES)))
    op.execute(findings.delete().where(findings.c.lens.in_(NEW_LENSES)))
    _lenses(LENSES_0094)
    op.drop_table(TABLE)
    runs = sa.table("runs", col("kind"), col("schedule_id"))
    op.execute(runs.delete().where(runs.c.kind == "review", runs.c.schedule_id.is_(None)))
    op.drop_constraint(REVIEW_CHECK, "runs", type_="check")
    op.create_check_constraint(REVIEW_CHECK, "runs", sa.or_(col("kind") != "review", col("schedule_id").is_not(None)))
