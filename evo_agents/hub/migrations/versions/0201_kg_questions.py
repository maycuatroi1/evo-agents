"""The run that writes a project's starter questions, and its lens.

Revision ID: 0201
Revises: 0200
Create Date: 2026-11-04

kg_init_runs gains ``questions_run_id`` (plan kg-upload-archive, step 4): the review run, lens kg_questions, that writes
the starter questions of a project whose data has no commit history to ask from (``evo_agents.hub.server.kg_questions``)
before the kg_init run measures them. It is null for every existing row and for a run that needs no questions; the run
it names may be deleted (SET NULL). What the questions run was asked and did stays in the row's ``body``.

findings.lens and proposals.lens take ``kg_questions``: the values below are copies of ``evo_agents.hub.review.LENSES``
as of this revision, never imports, so a later change to them takes a new revision. No other table changes.

Going back deletes the findings and the proposals of the lens (and those dropped as repeats of one), puts the checks of
0197 back and drops the column.
"""

import sqlalchemy as sa
from alembic import op

revision = "0201"
down_revision = "0200"
branch_labels = None
depends_on = None

TABLE = "kg_init_runs"
COLUMN = "questions_run_id"
LENSES_0197 = (
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
)
NEW_LENSES = ("kg_questions",)
LENSES = (*LENSES_0197, *NEW_LENSES)

col = sa.column


def _lenses(lenses) -> None:
    for table in ("findings", "proposals"):
        op.drop_constraint(f"{table}_lens_check", table, type_="check")
        op.create_check_constraint(f"{table}_lens_check", table, col("lens").in_(lenses))


def upgrade() -> None:
    op.add_column(TABLE, sa.Column(COLUMN, sa.BigInteger(), nullable=True))
    op.create_foreign_key(f"{TABLE}_{COLUMN}_fkey", TABLE, "runs", [COLUMN], ["id"], ondelete="SET NULL")
    op.create_index(f"{TABLE}_questions_run_idx", TABLE, [COLUMN])
    _lenses(LENSES)


def downgrade() -> None:
    findings = sa.table("findings", col("lens"))
    proposals = sa.table("proposals", col("id"), col("lens"), col("duplicate_of"))
    new = sa.select(proposals.c.id).where(proposals.c.lens.in_(NEW_LENSES))
    op.execute(proposals.delete().where(proposals.c.duplicate_of.in_(new)))  # dropped as repeats of one
    op.execute(proposals.delete().where(proposals.c.lens.in_(NEW_LENSES)))
    op.execute(findings.delete().where(findings.c.lens.in_(NEW_LENSES)))
    _lenses(LENSES_0197)
    op.drop_index(f"{TABLE}_questions_run_idx", table_name=TABLE)
    op.drop_constraint(f"{TABLE}_{COLUMN}_fkey", TABLE, type_="foreignkey")
    op.drop_column(TABLE, COLUMN)
