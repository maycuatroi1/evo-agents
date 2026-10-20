"""The Curator's lenses on the runtime and on the harness.

Revision ID: 0061
Revises: 0060
Create Date: 2026-10-20

``evo_agents.hub.review.LENSES`` gains ``runtime`` (the tokens, the prompt cache, the context, the compactions and the
cost of sessions, runs and merged pull requests, and the time of hooks and tools) and ``harness`` (what the harness puts
into every context: the always-loaded files, the instructions of MCP servers, the skill list, the text of hooks and the
output of tools); ``evo_agents.hub.runtime_metrics`` holds their figures and metrics. findings.lens and proposals.lens
take both. The values below are copies of ``review.LENSES`` as of this revision, never imports, so a later change to
them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

Going back deletes the findings and the proposals of the two lenses (and those dropped as repeats of one) and puts the
checks of 0060 back: schema 0060 knows neither lens. The playbook's entries keep their lens, which its check of 0060
takes.
"""

import sqlalchemy as sa
from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None

LENSES_0060 = (
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
)
NEW_LENSES = ("runtime", "harness")
LENSES = (*LENSES_0060, *NEW_LENSES)  # evo_agents.hub.review.LENSES, in another order

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
    _lenses(LENSES_0060)
