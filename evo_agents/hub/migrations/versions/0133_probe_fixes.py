"""The fix proposals and the replacement probes of the weekly probe report (plan correction-recurrence, step 4).

Revision ID: 0133
Revises: 0132
Create Date: 2026-10-24

The names below are copies of ``evo_agents.hub.tables`` and ``evo_agents.hub.tables_curator`` as of this revision,
never imports, so a later change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy
Core expressions.

proposals.fix_of names the merged change whose probe recurred on one model two weekly reports in a row and that the
proposal fixes; proposals_fix_open_key keeps one open (or deferred) fix proposal per change. findings.fix_of names the
change a review run was asked to fix and wrote a finding for instead, saying why. curator_probes.replaces names the
probe, not reproduced two weeks in a row, that a review run's probe replaced.

Going back drops the index and the three columns, and with them what they named.
"""

import sqlalchemy as sa
from alembic import op

revision = "0133"
down_revision = "0132"
branch_labels = None
depends_on = None

OPEN = ("open", "deferred")  # the states of a proposal still to answer

col = sa.column


def upgrade() -> None:
    op.add_column("proposals", sa.Column("fix_of", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "proposals_fix_of_fkey", "proposals", "curator_changes", ["fix_of"], ["id"], ondelete="SET NULL"
    )
    op.create_index(
        "proposals_fix_open_key",
        "proposals",
        ["fix_of"],
        unique=True,
        postgresql_where=col("fix_of").is_not(None) & col("state").in_(OPEN),
    )
    op.add_column("findings", sa.Column("fix_of", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "findings_fix_of_fkey", "findings", "curator_changes", ["fix_of"], ["id"], ondelete="SET NULL"
    )
    op.add_column("curator_probes", sa.Column("replaces", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "curator_probes_replaces_fkey", "curator_probes", "curator_probes", ["replaces"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint("curator_probes_replaces_fkey", "curator_probes", type_="foreignkey")
    op.drop_column("curator_probes", "replaces")
    op.drop_constraint("findings_fix_of_fkey", "findings", type_="foreignkey")
    op.drop_column("findings", "fix_of")
    op.drop_index("proposals_fix_open_key", table_name="proposals")
    op.drop_constraint("proposals_fix_of_fkey", "proposals", type_="foreignkey")
    op.drop_column("proposals", "fix_of")
