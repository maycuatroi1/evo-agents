"""The Curator's lens on the product.

Revision ID: 0087
Revises: 0086
Create Date: 2026-10-21

``evo_agents.hub.review.LENSES`` gains ``product`` (plan loop-engine, criterion a9): the signals the project measures,
the claims of its canvases and its goals with their metrics, through which the review run proposes claims and changes
of the autonomy policy. findings.lens and proposals.lens take it. The values below are copies of ``review.LENSES`` as
of this revision, never imports, so a later change to them takes a new revision. Every statement is an Alembic
operation on SQLAlchemy Core expressions. No column of a table changes.

Going back deletes the findings and the proposals of the lens (and those dropped as repeats of one) and puts the checks
of 0061 back: schema 0086 does not know it. The playbook's entries keep their lens, which its check of 0060 takes. What
the lens proposed through the plugins canvas and builder stays: those rows name a run, not a lens.
"""

import sqlalchemy as sa
from alembic import op

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None

LENSES_0086 = (  # as 0061 set them
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
)
NEW_LENSES = ("product",)
LENSES = (*LENSES_0086, *NEW_LENSES)  # evo_agents.hub.review.LENSES, in another order

col = sa.column


def _lenses(lenses) -> None:
    for table in ("findings", "proposals"):
        op.drop_constraint(f"{table}_lens_check", table, type_="check")
        op.create_check_constraint(f"{table}_lens_check", table, col("lens").in_(lenses))


def upgrade() -> None:
    _lenses(LENSES)


def downgrade() -> None:
    findings = sa.table("findings", col("lens"))
    proposals = sa.table("proposals", col("id"), col("lens"), col("duplicate_of"))
    new = sa.select(proposals.c.id).where(proposals.c.lens.in_(NEW_LENSES))
    op.execute(proposals.delete().where(proposals.c.duplicate_of.in_(new)))  # dropped as repeats of one
    op.execute(proposals.delete().where(proposals.c.lens.in_(NEW_LENSES)))
    op.execute(findings.delete().where(findings.c.lens.in_(NEW_LENSES)))
    _lenses(LENSES_0086)
