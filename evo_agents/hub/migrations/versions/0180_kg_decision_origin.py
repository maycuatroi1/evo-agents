"""The loop and the run a decision on the knowledge graph came from.

Revision ID: 0180
Revises: 0179
Create Date: 2026-10-28

The names below are copies of ``evo_agents.hub.tables_kg`` as of this revision, never imports, so a later change to
them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

The KG optimize loop (plan kg-retrieval-quality, criterion a6) applies the fixes of the review queue as decisions
(``POST /v1/kg/{project}/review/apply``). Each step of a decision's history says where it came from: origin, the loop
that took it (1 to 63 characters, as ``kg-optimize``), NULL for a step a member or a plugin took by hand; and run_id,
the run of that loop, NULL when it named none or once the run is gone. A check keeps a run to a step with an origin.
Every row of 0179 has neither, so it passes.

Going back drops both columns.
"""

import sqlalchemy as sa
from alembic import op

revision = "0180"
down_revision = "0179"
branch_labels = None
depends_on = None

EVENTS, ORIGIN, RUN = "kg_decision_events", "origin", "run_id"
MAX_ORIGIN_CHARS = 63

col = sa.column


def upgrade() -> None:
    op.add_column(EVENTS, sa.Column(ORIGIN, sa.Text()))
    op.add_column(EVENTS, sa.Column(RUN, sa.BigInteger()))
    op.create_foreign_key(f"{EVENTS}_{RUN}_fkey", EVENTS, "runs", [RUN], ["id"], ondelete="SET NULL")
    op.create_check_constraint(
        f"{EVENTS}_origin_check",
        EVENTS,
        sa.or_(
            sa.and_(col(ORIGIN).is_(None), col(RUN).is_(None)),
            sa.and_(col(ORIGIN).is_not(None), sa.func.char_length(col(ORIGIN)).between(1, MAX_ORIGIN_CHARS)),
        ),
    )


def downgrade() -> None:
    op.drop_constraint(f"{EVENTS}_origin_check", EVENTS, type_="check")
    op.drop_constraint(f"{EVENTS}_{RUN}_fkey", EVENTS, type_="foreignkey")
    op.drop_column(EVENTS, RUN)
    op.drop_column(EVENTS, ORIGIN)
