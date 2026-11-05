"""The runs that map a project's entities to its code, and their lens.

Revision ID: 0210
Revises: 0209
Create Date: 2026-11-06

kg_entity_map_runs holds each run that maps the entities of a project whose strategy is entity-graph-agent to its code
(plan kg-entities-to-code, step 2; ``evo_agents.hub.server.kg_entity_map_run``): what asked for it (the first build
under that strategy, citations gone stale, a call), the reasons, the build it read, the review run of the agent it
queued (lens kg_entity_map), the maps that run posted, the revision of each item the run's sink read when it was queued
(``revs``), and what it was shown (``body``). One run a project is in flight at a time (``kg_entity_map_runs_active_key``),
so a second call finds the first. The table starts empty.

findings.lens and proposals.lens take ``kg_entity_map``: the values below are copies of ``evo_agents.hub.review.LENSES``
as of this revision, never imports, so a later change to them takes a new revision. No other table changes.

Going back deletes the findings and the proposals of the lens (and those dropped as repeats of one), puts the checks of
0201 back and drops the table.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0210"
down_revision = "0209"
branch_labels = None
depends_on = None

TABLE = "kg_entity_map_runs"
ACTIVE = ("queued", "running")
STATES = (*ACTIVE, "done", "failed")
VIAS = ("first_build", "stale", "manual")
LENSES_0201 = (
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
    "kg_init",
    "kg_questions",
)
NEW_LENSES = ("kg_entity_map",)
LENSES = (*LENSES_0201, *NEW_LENSES)

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
        sa.Column("agent_run_id", sa.BigInteger(), nullable=True),
        sa.Column("maps", sa.Integer(), server_default="0", nullable=False),
        sa.Column("revs", postgresql.JSONB(), nullable=True),
        sa.Column("body", postgresql.JSONB(), nullable=False),
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


def downgrade() -> None:
    findings = sa.table("findings", col("lens"))
    proposals = sa.table("proposals", col("id"), col("lens"), col("duplicate_of"))
    new = sa.select(proposals.c.id).where(proposals.c.lens.in_(NEW_LENSES))
    op.execute(proposals.delete().where(proposals.c.duplicate_of.in_(new)))  # dropped as repeats of one
    op.execute(proposals.delete().where(proposals.c.lens.in_(NEW_LENSES)))
    op.execute(findings.delete().where(findings.c.lens.in_(NEW_LENSES)))
    _lenses(LENSES_0201)
    op.drop_index(f"{TABLE}_active_key", table_name=TABLE)
    op.drop_index(f"{TABLE}_agent_run_idx", table_name=TABLE)
    op.drop_index(f"{TABLE}_project_idx", table_name=TABLE)
    op.drop_table(TABLE)
